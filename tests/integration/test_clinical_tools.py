"""Real PostgreSQL/JWT/RBAC approval gate, row-lock races, citations and audit spans.

Only model computation is doubled. Identity resolution, authorization, ownership,
approvals, transactions, pgvector/FTS and final-note writes are real production code.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from pydantic import SecretStr

from app.application.auth.context import Principal
from app.application.clinical_tools.contracts import ToolName, ToolOwner, draft_digest
from app.application.clinical_tools.execution import ClinicalToolExecutor, ClinicalToolFactory
from app.application.clinical_tools.permissions import TOOL_POLICIES
from app.application.ports.llm import ToolCall
from app.core.config import AuthSettings, DatabaseSettings, LLMSettings, Settings
from app.core.container import Container
from app.domain.auth.value_objects import Role
from app.infrastructure.persistence.database import Database
from app.infrastructure.persistence.models import (
    ApprovalModel,
    ChunkEmbeddingModel,
    ChunkModel,
    DocumentModel,
    FinalClinicalNoteModel,
    SpanModel,
    TraceModel,
    UserModel,
    WorkflowRunModel,
)
from tests.integration.test_job_queue import migrate
from tests.support.clinical_tool_fakes import (
    DOSE_CLAIM,
    ORIGINAL_NOTE,
    REVIEWED_NOTE,
    arguments,
    bind,
)
from tests.support.knowledge_fakes import StubEmbeddings, StubLLM, StubReranker


@pytest.fixture(scope="module")
def tools_database_url() -> Iterator[str]:
    source = os.environ.get("TEST_DATABASE_URL")
    if not source:
        if os.environ.get("CI"):
            pytest.fail("CI must provide PostgreSQL for the clinical tool approval gate")
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL/pgvector service")
    name = "t18_tools_" + uuid4().hex
    admin = sa.engine.make_url(source).set(drivername="postgresql+psycopg")
    engine = sa.create_engine(admin, isolation_level="AUTOCOMMIT", hide_parameters=True)
    with engine.connect() as connection:
        connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
    try:
        url = admin.set(database=name)
        migrate(url.render_as_string(hide_password=False))
        yield url.set(drivername="postgresql").render_as_string(hide_password=False)
    finally:
        with engine.connect() as connection:
            connection.execute(sa.text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        engine.dispose()


@dataclass
class PgTools:
    container: Container
    database: Database
    factory: ClinicalToolFactory
    principal: Principal
    reviewer: Principal
    admin: Principal
    workflow_id: UUID
    other_workflow_id: UUID
    foreign_workflow_id: UUID
    approval_id: UUID
    chunk_id: UUID
    url: str

    def scope(self, owner: ToolOwner) -> ClinicalToolExecutor:
        return bind(self.factory, owner, self.principal, self.workflow_id)

    async def call(
        self,
        name: ToolName = ToolName.FINALIZE_CLINICAL_NOTE,
        *,
        owner: ToolOwner | None = None,
        data: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        executor = self.scope(owner or TOOL_POLICIES[name].owner)
        payload = data if data is not None else arguments(name, self.workflow_id, self.approval_id)
        result = await executor.execute(ToolCall("pg-call", name.value, json.dumps(payload)))
        return dict(json.loads(result.output))

    async def change_approval(self, **values: Any) -> None:
        async with self.database.session_factory() as session, session.begin():
            await session.execute(
                sa.update(ApprovalModel)
                .where(ApprovalModel.id == self.approval_id)
                .values(**values)
            )

    async def final_count(self) -> int:
        async with self.database.session_factory() as session:
            return int(
                await session.scalar(sa.select(sa.func.count()).select_from(FinalClinicalNoteModel))
                or 0
            )

    async def tool_span(self, trace_id: str) -> SpanModel:
        async with self.database.session_factory() as session:
            span = await session.scalar(
                sa.select(SpanModel).where(SpanModel.trace_id == UUID(trace_id))
            )
            assert span is not None
            return span


@pytest.fixture
async def pg_tools(
    tools_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[PgTools]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database=DatabaseSettings(url=tools_database_url),
        llm=LLMSettings(provider="ollama", fallback=None),
        auth=AuthSettings(
            secret_key=SecretStr("ticket18-test-only-signing-value"), seed_demo_accounts=False
        ),
    )
    container = Container(settings)
    assert container.database is not None
    database = container.database
    # These are the existing #7 ports. Nothing in authentication/authorization,
    # persistence or the actual #10 retrieval/grounding use cases is mocked.
    monkeypatch.setattr(container, "_embedding_provider", StubEmbeddings())
    monkeypatch.setattr(container, "_reranker", StubReranker())
    monkeypatch.setattr(container, "_llm_provider", StubLLM())
    owner_id, reviewer_id, admin_id, stranger_id = (uuid4() for _ in range(4))
    workflow_id, other_workflow_id, foreign_workflow_id = (uuid4() for _ in range(3))
    approval_id, document_id, chunk_id = (uuid4() for _ in range(3))
    async with database.session_factory() as session, session.begin():
        for identifier, role in (
            (owner_id, Role.ANALYST),
            (reviewer_id, Role.REVIEWER),
            (admin_id, Role.ADMIN),
            (stranger_id, Role.ANALYST),
        ):
            session.add(
                UserModel(
                    id=identifier,
                    email=f"{identifier}@example.com",
                    role=role.value,
                    hashed_password="synthetic-test-hash",
                )
            )
        await session.flush()
        for identifier, actor in (
            (workflow_id, owner_id),
            (other_workflow_id, owner_id),
            (foreign_workflow_id, stranger_id),
        ):
            session.add(
                WorkflowRunModel(
                    id=identifier,
                    user_id=actor,
                    correlation_id=str(identifier),
                    case_summary="Synthetic context only; no patient data.",
                    state="AWAITING_APPROVAL",
                )
            )
        await session.flush()
        session.add(
            ApprovalModel(
                id=approval_id,
                workflow_run_id=workflow_id,
                reviewer_id=reviewer_id,
                original_note=ORIGINAL_NOTE,
                approved_note=REVIEWED_NOTE,
                status="APPROVED",
            )
        )
        session.add(
            DocumentModel(
                id=document_id,
                user_id=owner_id,
                filename="synthetic-tool-evidence.md",
                content=DOSE_CLAIM,
                status="COMPLETED",
                metadata_={"classification": "synthetic"},
            )
        )
        await session.flush()
        session.add(
            ChunkModel(
                id=chunk_id,
                document_id=document_id,
                text=DOSE_CLAIM,
                metadata_={},
                section="Synthetic dosage example",
                page=1,
                document_version=1,
            )
        )
        await session.flush()
        session.add(
            ChunkEmbeddingModel(
                id=uuid4(),
                chunk_id=chunk_id,
                embedding=[1.0] + [0.0] * 383,
                embedding_model=settings.embedding.model,
                embedding_dim=384,
                embedding_version=settings.embedding.version,
            )
        )

    async def authenticate(identifier: UUID) -> Principal:
        # Deliberately overstated role claim: the real #5 resolver must reload PG.
        token = container.token_service.issue(subject=str(identifier), role="admin")
        return await container.resolve_principal_use_case().execute(token.access_token)

    try:
        principal = await authenticate(owner_id)
        assert principal.role == Role.ANALYST
        yield PgTools(
            container,
            database,
            container.clinical_tool_factory(),
            principal,
            await authenticate(reviewer_id),
            await authenticate(admin_id),
            workflow_id,
            other_workflow_id,
            foreign_workflow_id,
            approval_id,
            chunk_id,
            tools_database_url,
        )
    finally:
        # The database was uniquely CREATED above for this test module. No app or
        # developer database is ever selected for this cleanup.
        async with database.session_factory() as session, session.begin():
            await session.execute(
                sa.text(
                    "TRUNCATE final_clinical_notes, approvals, workflow_runs, "
                    "traces, documents, users CASCADE"
                )
            )
        await container.dispose()


@pytest.mark.parametrize("owner", list(ToolOwner))
@pytest.mark.parametrize("name", list(ToolName))
async def test_agent_scopes_are_enforced_with_real_identity_ownership_and_storage(
    pg_tools: PgTools, owner: ToolOwner, name: ToolName
) -> None:
    result = await pg_tools.call(name, owner=owner)
    allowed = TOOL_POLICIES[name].owner == owner
    assert result["ok"] is allowed, result
    if not allowed:
        assert result["error"]["code"] == "PERMISSION_DENIED"
        span = await pg_tools.tool_span(result["trace_id"])
        assert span.step_type == "tool" and span.status == "ERROR"
        assert span.outputs["outcome"] == "denied"
    assert await pg_tools.final_count() == int(allowed and name == ToolName.FINALIZE_CLINICAL_NOTE)


@pytest.mark.parametrize("status", ["PENDING", "REJECTED"])
async def test_nonapproved_persisted_decisions_cannot_finalize(
    pg_tools: PgTools, status: str
) -> None:
    await pg_tools.change_approval(status=status)
    result = await pg_tools.call()
    assert result["error"]["code"] == "APPROVAL_REQUIRED"
    assert await pg_tools.final_count() == 0
    span = await pg_tools.tool_span(result["trace_id"])
    assert span.outputs["approval_id"] == str(pg_tools.approval_id)
    assert span.outputs["error_code"] == "APPROVAL_REQUIRED"
    assert ORIGINAL_NOTE not in str(span.outputs) and REVIEWED_NOTE not in str(span.outputs)


async def test_no_persisted_approval_is_rejected(pg_tools: PgTools) -> None:
    async with pg_tools.database.session_factory() as session, session.begin():
        await session.execute(
            sa.delete(ApprovalModel).where(ApprovalModel.id == pg_tools.approval_id)
        )
    result = await pg_tools.call()
    assert result["error"]["code"] == "APPROVAL_REQUIRED"
    assert await pg_tools.final_count() == 0


@pytest.mark.parametrize("note", [None, "", "   "])
async def test_approval_without_explicit_reviewed_content_is_rejected(
    pg_tools: PgTools, note: str | None
) -> None:
    await pg_tools.change_approval(approved_note=note)
    result = await pg_tools.call()
    assert result["error"]["code"] == "APPROVAL_REQUIRED"
    assert await pg_tools.final_count() == 0


@pytest.mark.parametrize("mismatch", ["workflow", "draft", "bound_workflow"])
async def test_approval_must_match_both_workflow_and_exact_draft(
    pg_tools: PgTools, mismatch: str
) -> None:
    data = arguments(ToolName.FINALIZE_CLINICAL_NOTE, pg_tools.workflow_id, pg_tools.approval_id)
    if mismatch == "workflow":
        await pg_tools.change_approval(workflow_run_id=pg_tools.other_workflow_id)
    elif mismatch == "draft":
        await pg_tools.change_approval(original_note=ORIGINAL_NOTE + " Edited since review.")
    else:
        data["workflow_id"] = str(pg_tools.other_workflow_id)
    result = await pg_tools.call(data=data)
    assert result["error"]["code"] == "APPROVAL_MISMATCH"
    assert await pg_tools.final_count() == 0


@pytest.mark.parametrize("flag", ["approved", "role", "agent", "note"])
async def test_caller_claims_cannot_bypass_a_pending_approval(pg_tools: PgTools, flag: str) -> None:
    await pg_tools.change_approval(status="PENDING")
    data = arguments(ToolName.FINALIZE_CLINICAL_NOTE, pg_tools.workflow_id, pg_tools.approval_id)
    data[flag] = True if flag == "approved" else "forged"
    result = await pg_tools.call(data=data)
    assert result["error"]["code"] == "INVALID_ARGUMENTS"
    assert await pg_tools.final_count() == 0
    async with pg_tools.database.session_factory() as session:
        approval = await session.get(ApprovalModel, pg_tools.approval_id)
        assert approval is not None and approval.status == "PENDING"


async def test_approved_finalization_copies_only_reviewed_text_and_survives_a_new_pool(
    pg_tools: PgTools,
) -> None:
    result = await pg_tools.call()
    assert result["ok"] is True, result
    assert result["result"]["note"] == REVIEWED_NOTE != ORIGINAL_NOTE
    assert result["result"]["draft_id"] == draft_digest(ORIGINAL_NOTE)
    fresh = Database(pg_tools.url)
    try:
        async with fresh.session_factory() as session:
            note = await session.get(FinalClinicalNoteModel, UUID(result["result"]["note_id"]))
            assert note is not None
            assert note.note == REVIEWED_NOTE
            assert note.approval_id == pg_tools.approval_id
            assert note.workflow_run_id == pg_tools.workflow_id
            approval = await session.get(ApprovalModel, pg_tools.approval_id)
            workflow = await session.get(WorkflowRunModel, pg_tools.workflow_id)
            assert approval is not None and approval.status == "APPROVED"
            assert approval.original_note == ORIGINAL_NOTE
            # #18 is not the #19 decision workflow or the #17 state machine.
            assert workflow is not None and workflow.state == "AWAITING_APPROVAL"
    finally:
        await fresh.dispose()
    span = await pg_tools.tool_span(result["trace_id"])
    assert span.status == "OK" and span.outputs["note_id"] == result["result"]["note_id"]


async def test_read_tools_and_drafting_never_mutate_approvals_or_final_notes(
    pg_tools: PgTools,
) -> None:
    for name in ToolName:
        if name == ToolName.FINALIZE_CLINICAL_NOTE:
            continue
        assert (await pg_tools.call(name))["ok"] is True
        assert await pg_tools.final_count() == 0
    async with pg_tools.database.session_factory() as session:
        approval = await session.get(ApprovalModel, pg_tools.approval_id)
        assert approval is not None and approval.approved_note == REVIEWED_NOTE
        assert approval.original_note == ORIGINAL_NOTE and approval.status == "APPROVED"


async def test_real_dense_keyword_fusion_and_citations_resolve_to_pg_chunks(
    pg_tools: PgTools,
) -> None:
    result = await pg_tools.call(ToolName.SEARCH_CORPUS, data={"query": "Synthex-A dosage"})
    assert result["ok"] is True, result
    citation = result["result"]["citations"][0]
    assert citation["chunk_id"] == str(pg_tools.chunk_id)
    async with pg_tools.database.session_factory() as session:
        chunk = await session.get(ChunkModel, UUID(citation["chunk_id"]))
        assert chunk is not None and chunk.text == citation["text_snippet"]
        document = await session.get(DocumentModel, chunk.document_id)
        assert document is not None and document.filename == citation["document_name"]
        assert str(chunk.document_id) == citation["document_id"]
        assert chunk.page == citation["page"] and chunk.section == citation["section"]
        span = await session.scalar(
            sa.select(SpanModel).where(
                SpanModel.trace_id == UUID(result["result"]["evidence_trace_id"]),
                SpanModel.name == "retrieval.hybrid",
            )
        )
        assert span is not None
        assert (
            span.outputs["dense_count"]
            == span.outputs["keyword_count"]
            == span.outputs["fused_count"]
            == 1
        )
        assert span.outputs["rrf_k"] == 60


async def test_forged_admin_role_cannot_cross_workflow_ownership(pg_tools: PgTools) -> None:
    executor = pg_tools.factory.for_orchestrator(
        replace(pg_tools.principal, role=Role.ADMIN), pg_tools.foreign_workflow_id
    )
    result = json.loads(
        (await executor.execute(ToolCall("forged-role", "finalize_clinical_note", "{}"))).output
    )
    assert result["error"]["code"] == "PERMISSION_DENIED"
    assert await pg_tools.final_count() == 0
    async with pg_tools.database.session_factory() as session:
        trace = await session.get(TraceModel, UUID(result["trace_id"]))
        assert trace is not None and str(trace.user_id) == pg_tools.principal.user_id.value


async def test_role_demotion_after_binding_takes_effect_without_new_token(
    pg_tools: PgTools,
) -> None:
    executor = pg_tools.factory.for_orchestrator(pg_tools.admin, pg_tools.workflow_id)
    async with pg_tools.database.session_factory() as session, session.begin():
        await session.execute(
            sa.update(UserModel)
            .where(UserModel.id == UUID(pg_tools.admin.user_id.value))
            .values(role="analyst")
        )
    result = json.loads(
        (await executor.execute(ToolCall("demoted", "finalize_clinical_note", "{}"))).output
    )
    assert result["error"]["code"] == "PERMISSION_DENIED"
    assert await pg_tools.final_count() == 0


async def test_persisted_reviewer_must_have_existing_approval_permission(pg_tools: PgTools) -> None:
    await pg_tools.change_approval(reviewer_id=UUID(pg_tools.principal.user_id.value))
    result = await pg_tools.call()
    assert result["error"]["code"] == "PERMISSION_DENIED"
    assert await pg_tools.final_count() == 0


async def test_concurrent_replays_create_one_note_and_recheck_revocation(pg_tools: PgTools) -> None:
    results = await asyncio.gather(pg_tools.call(), pg_tools.call())
    assert all(result["ok"] for result in results), results
    assert len({result["result"]["note_id"] for result in results}) == 1
    assert sorted(result["result"]["created"] for result in results) == [False, True]
    assert await pg_tools.final_count() == 1
    await pg_tools.change_approval(status="REJECTED")
    rejected = await pg_tools.call()
    assert rejected["error"]["code"] == "APPROVAL_REQUIRED"
    assert await pg_tools.final_count() == 1


async def test_final_note_cannot_be_overwritten_by_later_approval_text(pg_tools: PgTools) -> None:
    first = await pg_tools.call()
    await pg_tools.change_approval(approved_note="A different reviewed candidate.")
    conflict = await pg_tools.call()
    assert conflict["error"]["code"] == "FINAL_NOTE_CONFLICT"
    async with pg_tools.database.session_factory() as session:
        note = await session.get(FinalClinicalNoteModel, UUID(first["result"]["note_id"]))
        assert note is not None and note.note == REVIEWED_NOTE


async def test_authoritative_decision_is_locked_and_read_before_final_note_write(
    pg_tools: PgTools,
) -> None:
    reached_approval = asyncio.Event()

    def observe_query(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if "FROM approvals" in statement and "FOR UPDATE" in statement:
            reached_approval.set()

    engine = pg_tools.database._engine.sync_engine
    sa.event.listen(engine, "before_cursor_execute", observe_query)
    task: asyncio.Task[dict[str, Any]] | None = None
    try:
        async with pg_tools.database.session_factory() as session, session.begin():
            # Hold an uncommitted revocation. A plain stale read would observe the
            # old APPROVED value; the real guarded writer must wait for this lock.
            await session.execute(
                sa.update(ApprovalModel)
                .where(ApprovalModel.id == pg_tools.approval_id)
                .values(status="PENDING")
            )
            task = asyncio.create_task(pg_tools.call())
            await asyncio.wait_for(reached_approval.wait(), timeout=10)
            assert not task.done()
            assert await pg_tools.final_count() == 0
        result = await asyncio.wait_for(task, timeout=10)
        assert result["error"]["code"] == "APPROVAL_REQUIRED"
        assert await pg_tools.final_count() == 0
    finally:
        sa.event.remove(engine, "before_cursor_execute", observe_query)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
