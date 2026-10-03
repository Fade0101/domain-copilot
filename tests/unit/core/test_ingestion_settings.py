"""Ingestion bounds and lazy registration on the production job runtime."""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from app.core.config import DatabaseSettings, IngestionSettings, Settings
from app.core.container import build_job_runtime


def test_chunk_defaults_match_the_design() -> None:
    settings = IngestionSettings()
    assert (settings.chunk_tokens, settings.chunk_overlap) == (512, 64)


@pytest.mark.parametrize(
    "values",
    [
        {"chunk_tokens": 64, "chunk_overlap": 64},
        {"max_upload_bytes": 0},
        {"max_pages": 0},
        {"max_chunks": 0},
        {"stage_timeout_seconds": 0},
    ],
)
def test_invalid_limits_are_rejected(values: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        IngestionSettings(**values)


async def test_default_job_runtime_registers_ingestion_without_loading_a_model() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        database=DatabaseSettings(url="postgresql://test@127.0.0.1/test"),
    )
    runtime = build_job_runtime(settings)
    try:
        job = runtime.service.prepare(
            "document.ingest", {"document_id": str(UUID(int=1))}, user_id=UUID(int=2)
        )
        assert job.operation_type == "document.ingest"
        assert runtime.ingestion is not None
        assert runtime.embeddings is not None and runtime.embeddings._model is None
    finally:
        await runtime.close()
