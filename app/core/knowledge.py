"""Shared wiring for the app and evaluation; retrieval/grounding logic stays in #10."""

from __future__ import annotations

from app.application.auth.authorization import AuthorizationService
from app.application.ports.embeddings import IEmbeddingProvider
from app.application.ports.reranking import IReranker
from app.application.ports.retrieval import IRetrievalStore
from app.application.ports.system import IIdGenerator
from app.application.retrieval.observability import RetrievalObserver
from app.application.retrieval.use_cases import HybridRetrievalUseCase, RetrievalOptions
from app.core.config import Settings


def build_hybrid_retrieval(
    settings: Settings,
    store: IRetrievalStore,
    embeddings: IEmbeddingProvider,
    reranker: IReranker,
    authorization: AuthorizationService,
    observer: RetrievalObserver,
    identifiers: IIdGenerator,
) -> HybridRetrievalUseCase:
    return HybridRetrievalUseCase(
        store,
        embeddings,
        reranker,
        authorization,
        observer,
        identifiers,
        RetrievalOptions(**settings.retrieval.model_dump()),
    )
