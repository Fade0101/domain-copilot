"""Durability boundaries, deterministic chunks and per-stage failures using only ports."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.application.documents.chunking import StructureAwareChunker, clean_blocks
from app.application.documents.embedding import IngestionEmbedder
from app.application.documents.ingestion_handler import DocumentIngestionHandler
from app.application.documents.ingestion_service import IngestionService
from app.application.errors import IngestionError, JobStoreError, UploadTooLargeError
from app.application.jobs.registry import JobHandlerRegistry
from app.application.jobs.runner import JobRunner
from app.application.jobs.service import JobService
from app.domain.documents.ingestion import IngestionOptions, IngestionStage, TextBlock
from app.domain.jobs.entities import JobState
from app.domain.shared.errors import InvariantViolationError
from tests.support.fakes import FakeRetrievalStore, FixedClock, SequentialIdGenerator
from tests.support.ingestion_fakes import (
    FakeExtractor,
    FakeIngestionStore,
    FakeTokenizer,
    RecordingEmbeddings,
)
from tests.support.job_fakes import FakeJobQueue, FakeJobStore

OWNER = UUID(int=900)
NOW = datetime(2026, 10, 2, tzinfo=UTC)
OPTIONS = IngestionOptions(
    chunk_tokens=16,
    chunk_overlap=4,
    embedding_batch_size=1,
    embedding_model="test-model",
    embedding_dim=4,
)


class InterruptCheckpointStore(FakeJobStore):
    interrupt = False

    async def checkpoint(self, job_id: UUID, data: dict[str, Any], now: datetime) -> None:
        if self.interrupt and "ingest:embed:0" in data:
            self.interrupt = False
            raise JobStoreError("simulated checkpoint connection loss")
        await super().checkpoint(job_id, data, now)


@dataclass
class Harness:
    jobs: InterruptCheckpointStore
    store: FakeIngestionStore
    queue: FakeJobQueue
    extractor: FakeExtractor
    tokenizer: FakeTokenizer
    embeddings: RecordingEmbeddings
    retrieval: FakeRetrievalStore
    service: IngestionService
    runner: JobRunner
    handler: DocumentIngestionHandler


@pytest.fixture
def ingestion() -> Harness:
    jobs = InterruptCheckpointStore()
    store = FakeIngestionStore(jobs)
    queue = FakeJobQueue(jobs)
    extractor = FakeExtractor()
    tokenizer = FakeTokenizer()
    embeddings = RecordingEmbeddings()
    retrieval = FakeRetrievalStore()
    clock = FixedClock(NOW)
    handler = DocumentIngestionHandler(
        store,
        extractor,
        StructureAwareChunker(tokenizer),
        IngestionEmbedder(embeddings, tokenizer),
        retrieval,
        clock,
        OPTIONS,
    )
    registry = JobHandlerRegistry([handler])
    service = IngestionService(
        store,
        JobService(jobs, queue, registry, clock, SequentialIdGenerator()),
        OPTIONS,
        max_upload_bytes=20_000,
    )
    return Harness(
        jobs,
        store,
        queue,
        extractor,
        tokenizer,
        embeddings,
        retrieval,
        service,
        JobRunner(jobs, registry, clock),
        handler,
    )


class TestSubmission:
    async def test_duplicates_share_a_job_and_keep_the_first_filename(
        self, ingestion: Harness
    ) -> None:
        first = await ingestion.service.submit("source.md", b"synthetic alpha", user_id=OWNER)
        repeated = await ingestion.service.submit("renamed.md", b"synthetic alpha", user_id=OWNER)
        assert repeated.reused and first.job.id == repeated.job.id
        assert repeated.document.filename == "source.md"
        assert len(ingestion.jobs.jobs) == 1
        await ingestion.runner.run(first.job.id)
        completed = await ingestion.service.submit("source.md", b"synthetic alpha", user_id=OWNER)
        assert completed.job.state == JobState.COMPLETED
        assert len(ingestion.embeddings.calls) == 1

    async def test_different_owners_and_source_versions_have_distinct_snapshots(
        self, ingestion: Harness
    ) -> None:
        original = await ingestion.service.submit("source.md", b"alpha", user_id=OWNER)
        other = await ingestion.service.submit("source.md", b"alpha", user_id=UUID(int=901))
        revision = await ingestion.service.submit("source.md", b"alpha", user_id=OWNER, version=2)
        assert len({original.document.id, other.document.id, revision.document.id}) == 3

    async def test_queue_loss_keeps_a_dispatchable_document_and_source(
        self, ingestion: Harness
    ) -> None:
        ingestion.queue.unavailable = True
        accepted = await ingestion.service.submit("source.md", b"alpha", user_id=OWNER)
        assert accepted.job.state == JobState.QUEUED
        assert await ingestion.store.source(accepted.document.id) == b"alpha"
        assert await ingestion.jobs.dispatchable(10) == [accepted.job.id]

    @pytest.mark.parametrize(
        "filename", ["source.txt", "../source.md", "C:\\source.pdf", "bad\x00.md"]
    )
    async def test_invalid_source_names_create_no_job(
        self, ingestion: Harness, filename: str
    ) -> None:
        with pytest.raises(InvariantViolationError):
            await ingestion.service.submit(filename, b"alpha", user_id=OWNER)
        assert not ingestion.jobs.jobs

    async def test_size_and_signature_checks_happen_before_acceptance(
        self, ingestion: Harness
    ) -> None:
        with pytest.raises(UploadTooLargeError):
            await ingestion.service.submit("big.md", b"x" * 20_001, user_id=OWNER)
        with pytest.raises(InvariantViolationError):
            await ingestion.service.submit("fake.pdf", b"not pdf", user_id=OWNER)
        assert not ingestion.store.documents


class TestPipeline:
    async def test_all_stages_finish_with_citation_metadata(self, ingestion: Harness) -> None:
        accepted = await ingestion.service.submit(
            "source.md", b"Synthetic alpha content", user_id=OWNER, version=3
        )
        await ingestion.runner.run(accepted.job.id)
        document = await ingestion.store.get(accepted.document.id)
        assert document.status == "COMPLETED" and document.ingested_at == NOW
        assert {state["status"] for state in document.stages.values()} == {"COMPLETED"}
        record = next(iter(ingestion.retrieval.records.values()))
        assert (record.document_version, record.page, record.section, record.ingested_at) == (
            3,
            1,
            "Synthetic",
            NOW,
        )
        assert record.embedding_model == "test-model" and record.embedding_dim == 4
        job = ingestion.jobs.jobs[accepted.job.id]
        assert job.state == JobState.COMPLETED
        assert job.result_payload is not None
        assert job.result_payload["chunk_count"] == 1
        assert "Synthetic alpha content" not in json.dumps(job.checkpoint_data)
        assert job.input_payload == {"document_id": str(document.id)}

    @pytest.mark.parametrize("stage", list(IngestionStage))
    async def test_each_stage_records_a_safe_failure(
        self, ingestion: Harness, stage: IngestionStage
    ) -> None:
        unsafe = RuntimeError("secret source content must never become an error")
        if stage == IngestionStage.EXTRACT:
            ingestion.extractor.failure = unsafe
        elif stage == IngestionStage.CLEAN:
            ingestion.extractor.blocks = [TextBlock("\x00 \t")]
        elif stage == IngestionStage.CHUNK:
            ingestion.tokenizer.failure = unsafe
        elif stage == IngestionStage.EMBED:
            ingestion.embeddings.failure = unsafe
        else:

            async def fail_index(records: list) -> None:
                raise unsafe

            ingestion.retrieval.upsert_chunks = fail_index  # type: ignore[method-assign]
        accepted = await ingestion.service.submit("source.md", b"alpha", user_id=OWNER)
        await ingestion.runner.run(accepted.job.id)
        document = await ingestion.store.get(accepted.document.id)
        assert document.status == "FAILED" and document.error_stage == stage.value
        assert document.stages[stage.value]["status"] == "FAILED"
        assert document.error_message and "secret" not in document.error_message
        assert ingestion.jobs.jobs[accepted.job.id].state == JobState.FAILED
        later = list(IngestionStage)[list(IngestionStage).index(stage) + 1 :]
        assert all(document.stages[item.value]["status"] == "PENDING" for item in later)

    async def test_restart_skips_an_embedding_saved_before_the_job_checkpoint(
        self, ingestion: Harness
    ) -> None:
        source = " ".join(f"word{index}" for index in range(40)).encode()
        accepted = await ingestion.service.submit("source.md", source, user_id=OWNER)
        ingestion.jobs.interrupt = True
        with pytest.raises(JobStoreError):
            await ingestion.runner.run(accepted.job.id)
        assert ingestion.jobs.jobs[accepted.job.id].state == JobState.STARTED
        assert (accepted.document.id, "embed:0") in ingestion.store.artifacts
        assert "ingest:embed:0" not in ingestion.jobs.jobs[accepted.job.id].checkpoint_data
        restarted = JobRunner(
            ingestion.jobs, JobHandlerRegistry([ingestion.handler]), FixedClock(NOW)
        )
        await restarted.run(accepted.job.id)
        assert ingestion.extractor.calls == 1
        assert len(ingestion.embeddings.calls) == 3
        assert len(ingestion.retrieval.records) == 3
        assert ingestion.jobs.jobs[accepted.job.id].state == JobState.COMPLETED

    async def test_retry_of_a_failed_document_reuses_completed_stages(
        self, ingestion: Harness
    ) -> None:
        ingestion.embeddings.failure = RuntimeError("offline")
        accepted = await ingestion.service.submit("source.md", b"alpha", user_id=OWNER)
        await ingestion.runner.run(accepted.job.id)
        ingestion.embeddings.failure = None
        retry = await ingestion.service.submit("source.md", b"alpha", user_id=OWNER)
        assert retry.job.id != accepted.job.id and retry.document.id == accepted.document.id
        await ingestion.runner.run(retry.job.id)
        assert ingestion.extractor.calls == 1
        assert (await ingestion.store.get(retry.document.id)).status == "COMPLETED"

    async def test_index_commit_before_checkpoint_replays_without_duplicate_chunks(
        self, ingestion: Harness
    ) -> None:
        original = ingestion.store.save_artifact
        interrupted = False

        async def fail_after_index(
            document_id: UUID,
            key: str,
            data: dict[str, Any],
            stage: IngestionStage,
            now: datetime,
            *,
            items: int = 0,
            complete: bool = False,
        ) -> None:
            nonlocal interrupted
            if key == "index:0" and not interrupted:
                interrupted = True
                raise JobStoreError("Lost connection after retrieval commit")
            await original(document_id, key, data, stage, now, items=items, complete=complete)

        ingestion.store.save_artifact = fail_after_index  # type: ignore[method-assign]
        accepted = await ingestion.service.submit("source.md", b"Synthetic alpha", user_id=OWNER)
        with pytest.raises(JobStoreError):
            await ingestion.runner.run(accepted.job.id)
        identifiers = set(ingestion.retrieval.records)
        assert len(identifiers) == 1
        await ingestion.runner.run(accepted.job.id)
        assert set(ingestion.retrieval.records) == identifiers
        assert len(ingestion.embeddings.calls) == 1

    async def test_retry_after_final_cancellation_restores_completed_stage_status(
        self, ingestion: Harness
    ) -> None:
        checkpoint = ingestion.jobs.checkpoint

        async def cancel_after_index(job_id: UUID, data: dict[str, Any], now: datetime) -> None:
            await checkpoint(job_id, data, now)
            if "ingest:index" in data:
                ingestion.jobs.jobs[job_id] = replace(
                    ingestion.jobs.jobs[job_id], cancellation_requested=True
                )

        ingestion.jobs.checkpoint = cancel_after_index  # type: ignore[method-assign]
        accepted = await ingestion.service.submit("source.md", b"Synthetic alpha", user_id=OWNER)
        await ingestion.runner.run(accepted.job.id)
        assert ingestion.jobs.jobs[accepted.job.id].state == JobState.CANCELLED
        identifiers = set(ingestion.retrieval.records)
        assert len(identifiers) == 1

        ingestion.jobs.checkpoint = checkpoint  # type: ignore[method-assign]
        retry = await ingestion.service.submit("source.md", b"Synthetic alpha", user_id=OWNER)
        await ingestion.runner.run(retry.job.id)

        document = await ingestion.store.get(retry.document.id)
        assert document.status == "COMPLETED"
        assert {stage["status"] for stage in document.stages.values()} == {"COMPLETED"}
        assert len(ingestion.embeddings.calls) == 1
        assert set(ingestion.retrieval.records) == identifiers

    async def test_unattached_job_cannot_change_a_document(self, ingestion: Harness) -> None:
        accepted = await ingestion.service.submit("source.md", b"alpha", user_id=OWNER)
        original = ingestion.jobs.jobs[accepted.job.id]
        extra = replace(original, id=UUID(int=999))
        await ingestion.jobs.add(extra)
        await ingestion.runner.run(extra.id)
        assert ingestion.jobs.jobs[extra.id].state == JobState.FAILED
        assert (await ingestion.store.get(accepted.document.id)).status == "PENDING"
        assert ingestion.extractor.calls == 0


class TestChunking:
    async def test_default_windows_are_512_tokens_with_exact_64_token_overlap(self) -> None:
        words = [f"token{index}" for index in range(1350)]
        chunks = await StructureAwareChunker(FakeTokenizer()).chunk(
            OWNER, [TextBlock(" ".join(words))], IngestionOptions()
        )
        assert [chunk.token_count for chunk in chunks] == [512, 512, 454]
        assert chunks[0].text.split()[-64:] == chunks[1].text.split()[:64]
        assert chunks[1].text.split()[-64:] == chunks[2].text.split()[:64]

    async def test_page_and_heading_boundaries_do_not_share_overlap(self) -> None:
        blocks = [
            TextBlock("alpha " * 30, 1, ("Guide", "Overview")),
            TextBlock("beta " * 30, 2, ("Guide", "Follow up")),
        ]
        chunker = StructureAwareChunker(FakeTokenizer())
        chunks = await chunker.chunk(OWNER, blocks, OPTIONS)
        assert all("beta" not in chunk.text for chunk in chunks if chunk.page == 1)
        assert all("alpha" not in chunk.text for chunk in chunks if chunk.page == 2)
        assert {chunk.section for chunk in chunks} == {"Guide > Overview", "Guide > Follow up"}
        assert [chunk.id for chunk in chunks] == [
            chunk.id for chunk in await chunker.chunk(OWNER, blocks, OPTIONS)
        ]
        changed = await chunker.chunk(OWNER, blocks, replace(OPTIONS, chunk_tokens=20))
        assert chunks[0].id != changed[0].id

    def test_cleaning_preserves_clinical_symbols_and_values(self) -> None:
        blocks = clean_blocks(
            [TextBlock("\x00  Synthetic\t5 mg ≤ 10 µg\r\nβ value\r\n", 2, ("Heading",))]
        )
        assert blocks == [TextBlock("Synthetic 5 mg ≤ 10 µg\nβ value", 2, ("Heading",))]


class TestEmbeddingCoverage:
    async def test_long_chunks_include_the_tail_in_embedding_input(self) -> None:
        provider = RecordingEmbeddings()
        embedder = IngestionEmbedder(provider, FakeTokenizer(embedding_limit=4))
        vectors = await embedder.embed(["alpha one two three four five six beta"], OPTIONS)
        assert provider.calls == [["alpha one two three"], ["four five six beta"]]
        assert vectors[0][1] == vectors[0][2] > 0

    async def test_bad_provider_provenance_fails_before_indexing(self) -> None:
        with pytest.raises(IngestionError, match="provenance"):
            await IngestionEmbedder(RecordingEmbeddings(model="wrong"), FakeTokenizer()).embed(
                ["alpha"], OPTIONS
            )
