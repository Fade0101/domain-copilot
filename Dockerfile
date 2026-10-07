FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/app/.cache/huggingface

WORKDIR /app
COPY requirements.txt ./
# The assessment runs local embeddings on CPU; avoid downloading CUDA runtimes.
RUN pip install --default-timeout=1000 torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --default-timeout=1000 --retries 10 -r requirements.txt

RUN groupadd --system copilot && useradd --system --gid copilot --home-dir /app copilot
RUN mkdir -p /app/.cache/huggingface && chown -R copilot:copilot /app/.cache
COPY --chown=copilot:copilot app ./app
COPY --chown=copilot:copilot migrations ./migrations
COPY --chown=copilot:copilot prompts ./prompts
COPY --chown=copilot:copilot alembic.ini ./
COPY --chown=copilot:copilot scripts/ingest_documents.py ./scripts/ingest_documents.py
COPY --chown=copilot:copilot scripts/corpus.py scripts/evaluate.py ./scripts/
COPY --chown=copilot:copilot data ./data
USER copilot

CMD ["celery", "-A", "app.core.worker:celery_app", "worker", "--loglevel=INFO", "--concurrency=2"]
