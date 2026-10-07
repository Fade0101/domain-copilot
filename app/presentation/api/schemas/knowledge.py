"""Validated request/response contracts for synchronous retrieval and grounded ask."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.application.retrieval.use_cases import MAX_QUERY_CHARACTERS


class RetrieveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=MAX_QUERY_CHARACTERS)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=MAX_QUERY_CHARACTERS)
    stream: bool = Field(default=False, strict=True, description="Return grounded SSE when true.")
    session_id: UUID | None = Field(
        default=None, description="Persist this exchange in the caller's own session."
    )


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


class AnswerResponse(AskResponse):
    """A grounded answer. Citation fields are exactly the Ticket #10 contract."""

    refused: Literal[False]
    citations: list[CitationResponse] = Field(min_length=1)


class RefusalResponse(AskResponse):
    """Successful safe refusal, returned with HTTP 200 rather than a server failure."""

    answer: Literal["Not enough information in the corpus"]
    refused: Literal[True]
    citations: list[CitationResponse] = Field(max_length=0)


AskOutcome = Annotated[AnswerResponse | RefusalResponse, Field(discriminator="refused")]
ask_outcome: TypeAdapter[AnswerResponse | RefusalResponse] = TypeAdapter(AskOutcome)
