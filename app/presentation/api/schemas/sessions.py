"""Owner-only session metadata and ordered question/answer history."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.presentation.api.schemas.knowledge import AskOutcome


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=255)


class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    user_id: UUID
    title: str
    created_at: datetime


class SessionListResponse(BaseModel):
    items: list[SessionResponse]
    limit: int
    offset: int


class MessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    session_id: UUID
    sequence: int = Field(ge=1)
    role: Literal["user", "assistant"]
    content: str
    answer: AskOutcome | None = Field(
        description="Grounded outcome for assistant messages; null for user messages."
    )
    created_at: datetime


class MessageListResponse(BaseModel):
    items: list[MessageResponse]
    limit: int
    offset: int
