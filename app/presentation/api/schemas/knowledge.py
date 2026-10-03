"""Validated request/response contracts for synchronous retrieval and grounded ask."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.application.retrieval.use_cases import MAX_QUERY_CHARACTERS


class RetrieveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARACTERS)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=MAX_QUERY_CHARACTERS)


class CitationResponse(BaseModel):
    """AC-2.5: exactly seven fields; score is sigmoid(BGE cross-encoder logit)."""

    model_config = ConfigDict(from_attributes=True)
    document_id: UUID
    document_name: str
    section: str | None
    page: int | None
    chunk_id: UUID
    relevance_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    text_snippet: str


class RetrieveResponse(BaseModel):
    trace_id: UUID
    citations: list[CitationResponse]


class AskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    answer: str
    citations: list[CitationResponse]
    refused: bool
    trace_id: UUID
