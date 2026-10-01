FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./
# The assessment runs local embeddings on CPU; avoid downloading CUDA runtimes.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r requirements.txt

RUN groupadd --system copilot && useradd --system --gid copilot --home-dir /app copilot
COPY --chown=copilot:copilot app ./app
COPY --chown=copilot:copilot migrations ./migrations
COPY --chown=copilot:copilot prompts ./prompts
COPY --chown=copilot:copilot alembic.ini ./
USER copilot

CMD ["celery", "-A", "app.core.worker:celery_app", "worker", "--loglevel=INFO", "--concurrency=2"]
