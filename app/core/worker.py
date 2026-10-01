"""Worker entrypoint: celery -A app.core.worker:celery_app worker --loglevel=INFO."""

from app.core.container import get_job_runtime

celery_app = get_job_runtime().celery_app
