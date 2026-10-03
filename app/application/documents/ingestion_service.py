"""Bounded source acceptance and idempotent dispatch on the real T7 job service."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from app.application.errors import UploadTooLargeError
from app.application.jobs.service import JobService
from app.application.ports.ingestion import IIngestionStore, IngestionDocument, IngestionUpload
from app.domain.documents.ingestion import IngestionOptions, document_identity
from app.domain.jobs.entities import Job
from app.domain.shared.errors import InvariantViolationError

INGEST_OPERATION = "document.ingest"


def source_media_type(filename: str, supplied: str) -> str:
    if (
        not filename.strip()
        or len(filename) > 255
        or any(char in filename for char in ("/", "\\", ":", "\x00"))
        or any(ord(char) < 32 for char in filename)
        or filename in {".", ".."}
    ):
        raise InvariantViolationError("Supply a plain source filename without a path.")
    extension = filename.rsplit(".", 1)[-1].lower()
    expected = {"pdf": "application/pdf", "md": "text/markdown", "markdown": "text/markdown"}.get(
        extension
    )
    if expected is None:
        raise InvariantViolationError("Only PDF and Markdown sources are supported.")
    supplied = supplied.split(";", 1)[0].strip().lower()
    allowed = {expected, "application/octet-stream", ""}
    if expected == "text/markdown":
        allowed.add("text/plain")
    if supplied not in allowed:
        raise InvariantViolationError("The content type does not match the source filename.")
    return expected


@dataclass(frozen=True, slots=True)
class IngestionAccepted:
    document: IngestionDocument
    job: Job
    reused: bool


class IngestionService:
    def __init__(
        self,
        store: IIngestionStore,
        jobs: JobService,
        options: IngestionOptions,
        *,
        max_upload_bytes: int,
    ) -> None:
        self.store = store
        self._jobs = jobs
        self._options = options
        self.max_upload_bytes = max_upload_bytes

    async def submit(
        self,
        filename: str,
        source: bytes,
        *,
        user_id: UUID,
        media_type: str = "",
        version: int = 1,
    ) -> IngestionAccepted:
        media_type = source_media_type(filename, media_type)
        if not source:
            raise InvariantViolationError("The source file is empty.")
        if len(source) > self.max_upload_bytes:
            raise UploadTooLargeError("The source file exceeds the upload limit.")
        if isinstance(version, bool) or not 1 <= version <= 2_147_483_647:
            raise InvariantViolationError("Document version must be a positive 32-bit integer.")
        if media_type == "application/pdf" and not source.lstrip().startswith(b"%PDF-"):
            raise InvariantViolationError("The source is not a PDF file.")
        digest = hashlib.sha256(source).hexdigest()
        document_id = document_identity(user_id, digest, media_type, version)
        job = self._jobs.prepare(
            INGEST_OPERATION, {"document_id": str(document_id)}, user_id=user_id
        )
        document = await self.store.accept(
            IngestionUpload(
                document_id, user_id, filename, digest, media_type, version, source, self._options
            ),
            job,
        )
        # The source and job already share a committed PG transaction. Redis loss
        # cannot strand a source without its reconcilable PENDING/QUEUED job.
        dispatched = await self._jobs.dispatch(document.job_id)
        return IngestionAccepted(document, dispatched, reused=document.job_id != job.id)
