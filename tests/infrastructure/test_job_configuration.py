"""Queue adapter configuration cannot switch transport or expose URL credentials."""

from __future__ import annotations

import secrets

import pytest

from app.application.errors import ConfigurationError
from app.core.config import DatabaseSettings, QueueSettings
from app.infrastructure.queue.celery_queue import create_celery_app


def test_job_connection_urls_are_hidden_in_configuration_repr() -> None:
    credential = secrets.token_hex(16)
    settings = QueueSettings(broker_url=f"redis://:{credential}@localhost/0")
    database = DatabaseSettings(url=f"postgresql://local:{credential}@localhost/test")
    assert credential not in repr(settings) and credential not in repr(database)


def test_a_non_redis_transport_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="Redis"):
        create_celery_app("memory://", "test", visibility_timeout=60, connection_timeout=1)


def test_an_inherited_result_backend_cannot_store_job_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CELERY_RESULT_BACKEND", "redis://localhost/9")
    app = create_celery_app(
        "redis://localhost/0", "test", visibility_timeout=60, connection_timeout=1
    )
    try:
        assert app.backend.__class__.__name__ == "DisabledBackend"
        assert app.conf.task_ignore_result and not app.conf.task_store_errors_even_if_ignored
    finally:
        app.close()
