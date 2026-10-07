"""Separate real Celery process for browser tests; no application-only test routes."""

import asyncio
import os
import sys

from app.application.jobs.diagnostic import DiagnosticJobHandler
from app.core.config import Settings
from app.core.container import build_job_runtime
from tests.web.providers import BrowserClinicalHandler

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    settings = Settings.model_validate_json(os.environ["WEB_TEST_SETTINGS"])
    runtime = build_job_runtime(
        settings, handlers=[DiagnosticJobHandler(), BrowserClinicalHandler(settings)]
    )
    runtime.celery_app.worker_main(
        ["worker", "--pool=solo", "--concurrency=1", "--loglevel=WARNING", "--without-mingle"]
    )
