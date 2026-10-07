"""Small provider/store doubles for retrieval and grounded Q&A contract tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from app.application.auth.authorization import AuthorizationService
from app.application.auth.context import Principal
from app.application.ports.embeddings import EmbeddingResult, IEmbeddingProvider
from app.application.ports.llm import (
    CompletionRequest,
    CompletionResponse,
    ILLMProvider,
    StreamChunk,
)
from app.application.ports.prompts import IPromptProvider, Prompt
from app.application.ports.reranking import IReranker, RerankScore
from app.application.ports.retrieval import SearchHit
from app.application.qa.use_cases import AskUseCase
from app.application.retrieval.observability import RetrievalObserver
from app.application.retrieval.use_cases import HybridRetrievalUseCase, RetrievalOptions
from app.domain.auth.value_objects import EmailAddress, Role, UserId
from tests.support.fakes import (
    FakeOwnershipQuery,
    FakeRetrievalStore,
    FixedClock,
    RecordingAuditSink,
    SequentialIdGenerator,
)


def hit(
    number: int, text: str = "The synthetic clinic opens on Monday.", score: float = 0.9
) -> SearchHit:
    return SearchHit(
        document_id=UUID(int=100 + number),
        chunk_id=UUID(int=number),
        document_name=f"synthetic-{number}.md",
        section="Opening hours",
        page=None,
        snippet=text,
        score=score,
        embedding_model=None,
        embedding_dim=None,
        embedding_version=None,
    )


def principal() -> Principal:
    return Principal(
        UserId(str(UUID(int=999))), EmailAddress("synthetic@example.com"), Role.ANALYST
    )


class StaticRetrievalStore(FakeRetrievalStore):
    def __init__(self, dense: list[SearchHit], keyword: list[SearchHit]) -> None:
        super().__init__()
        self.dense = dense
        self.keyword = keyword
        self.calls: list[tuple[str, int]] = []

    async def dense_search(self, embedding: list[float], *, top_k: int) -> list[SearchHit]:
        self.calls.append(("dense", top_k))
        return self.dense[:top_k]

    async def keyword_search(self, query: str, *, top_k: int) -> list[SearchHit]:
        self.calls.append(("keyword", top_k))
        return self.keyword[:top_k]


class StubEmbeddings(IEmbeddingProvider):
    async def generate_embeddings(self, texts: list[str]) -> EmbeddingResult:
        return EmbeddingResult([[1.0] + [0.0] * 383 for _ in texts], "all-MiniLM-L6-v2", 384)


class StubReranker(IReranker):
    def __init__(self, scores: dict[UUID, float] | None = None) -> None:
        self.scores = scores or {}
        self.calls: list[list[SearchHit]] = []
        self.override: list[RerankScore] | None = None

    @property
    def model_name(self) -> str:
        return "test-cross-encoder"

    def close(self) -> None:
        pass

    async def rerank(self, query: str, candidates: list[SearchHit]) -> list[RerankScore]:
        self.calls.append(candidates)
        if self.override is not None:
            return self.override
        return [
            RerankScore(item.chunk_id, self.scores.get(item.chunk_id, 0.9)) for item in candidates
        ]


class StubLLM(ILLMProvider):
    def __init__(self, content: str | None = "auto") -> None:
        self.content = content
        self.calls: list[CompletionRequest] = []
        self.failure: Exception | None = None

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.calls.append(request)
        if self.failure is not None:
            raise self.failure
        content = self.content
        if content == "auto":
            data = json.loads(request.messages[1]["content"])
            content = json.dumps(
                {
                    "status": "supported",
                    "chunk_ids": [item["chunk_id"] for item in data["evidence"]],
                }
            )
        return CompletionResponse(content=content, usage={"total_tokens": 12})

    def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        raise AssertionError("Ticket #10 must not use streaming")


class StubPrompts(IPromptProvider):
    def get(self, prompt_id: str, version: int | str | None = None) -> Prompt:
        assert prompt_id == "grounded_answer" and version == 2
        return Prompt(prompt_id, 2, "Test policy", (), "TRUSTED_SYSTEM_POLICY")


@dataclass
class KnowledgeHarness:
    retrieval: HybridRetrievalUseCase
    ask: AskUseCase
    store: StaticRetrievalStore
    reranker: StubReranker
    llm: StubLLM
    audit: RecordingAuditSink


def harness(
    dense: list[SearchHit] | None = None,
    keyword: list[SearchHit] | None = None,
    *,
    scores: dict[UUID, float] | None = None,
    options: RetrievalOptions = RetrievalOptions(),
    content: str | None = "auto",
) -> KnowledgeHarness:
    audit = RecordingAuditSink()
    observer = RetrievalObserver(audit, FixedClock(datetime(2026, 1, 1, tzinfo=UTC)))
    identifiers = SequentialIdGenerator()
    store = StaticRetrievalStore(dense or [], keyword or [])
    reranker = StubReranker(scores)
    llm = StubLLM(content)
    retrieval = HybridRetrievalUseCase(
        store,
        StubEmbeddings(),
        reranker,
        AuthorizationService(FakeOwnershipQuery()),
        observer,
        identifiers,
        options,
    )
    ask = AskUseCase(retrieval, llm, StubPrompts(), observer, identifiers)
    return KnowledgeHarness(retrieval, ask, store, reranker, llm, audit)
