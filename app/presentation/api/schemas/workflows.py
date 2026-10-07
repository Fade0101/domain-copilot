"""Minimal HTTP projections for the existing clinical workflow service."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.application.clinical_tools.contracts import MAX_CASE_CHARACTERS
from app.application.retrieval.use_cases import MAX_QUERY_CHARACTERS
from app.domain.workflow.entities import WorkflowRun
from app.domain.workflow.state import ClinicalWorkflowState
from app.presentation.api.schemas.jobs import JobAcceptedResponse


class StartWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    clinical_question: str = Field(min_length=1, max_length=MAX_QUERY_CHARACTERS, strict=True)
    case_summary: str = Field(min_length=1, max_length=MAX_CASE_CHARACTERS, strict=True)
    patient_context: str = Field(default="", max_length=MAX_CASE_CHARACTERS, strict=True)


class WorkflowAcceptedResponse(JobAcceptedResponse):
    workflow_id: UUID


class WorkflowStatusResponse(BaseModel):
    workflow_id: UUID
    owner_id: UUID
    correlation_id: str
    job_id: UUID | None
    state: ClinicalWorkflowState
    created_at: datetime

    @classmethod
    def from_run(cls, run: WorkflowRun) -> "WorkflowStatusResponse":
        return cls(
            workflow_id=run.id,
            owner_id=run.user_id,
            correlation_id=run.correlation_id,
            job_id=run.approval_job_id,
            state=run.state,
            created_at=run.created_at,
        )
