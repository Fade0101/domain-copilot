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


def test_an_inherited_broker_url_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # Celery prefers CELERY_BROKER_URL over the configured value and the property
    # re-reads the environment on every access, so it cannot be neutralised the way
    # the result backend is. Silently honouring it would have the API publish job
    # ids to a broker the worker never consumes: jobs sit QUEUED, reconcile()
    # republishes to the same wrong broker, and settings.queue.broker_url still
    # reports the configured value. Failing at startup is the only safe answer.
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://inherited:6379/9")
    with pytest.raises(ConfigurationError, match="CELERY_BROKER_URL"):
        create_celery_app(
            "redis://localhost:6379/0", "test", visibility_timeout=60, connection_timeout=1
        )


def test_a_broker_url_matching_the_environment_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Guards against a false positive: deployments that legitimately export the
    # same value must still boot.
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    app = create_celery_app(
        "redis://localhost:6379/0", "test", visibility_timeout=60, connection_timeout=1
    )
    try:
        assert app.conf.broker_url == "redis://localhost:6379/0"
    finally:
        app.close()


def test_the_configured_broker_is_used_when_the_environment_is_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CELERY_BROKER_URL", raising=False)
    app = create_celery_app(
        "redis://localhost:6379/3", "test", visibility_timeout=60, connection_timeout=1
    )
    try:
        assert app.conf.broker_url == "redis://localhost:6379/3"
    finally:
        app.close()
