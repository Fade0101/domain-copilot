"""Capture actual software/model identities and non-secret execution settings."""

from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
from huggingface_hub import try_to_load_from_cache

from app.application.evaluation.data import EvaluationSetupError
from app.infrastructure.prompts.yaml_prompt_provider import YamlPromptProvider


def source_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    paths = sorted(
        [
            *root.glob("app/**/*.py"),
            *root.glob("scripts/*.py"),
            *root.glob("prompts/*.yaml"),
            root / "requirements.txt",
            root / "pyproject.toml",
        ],
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in paths:
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode() + b"\0")
            digest.update(path.read_text(encoding="utf-8").encode() + b"\0")
    return digest.hexdigest()


class RuntimeEvaluationVersions:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        root: Path,
        ollama_url: str,
        prompt_directory: str = "prompts",
    ) -> None:
        self._config = config
        self._root = root
        self._ollama_url = ollama_url
        self._prompt_directory = prompt_directory

    async def capture(self) -> dict[str, Any]:
        llm = self._config["llm"]
        if llm["fallback"] and llm["fallback"] != llm["provider"]:
            raise EvaluationSetupError("EVALUATION_REQUIRES_SINGLE_PROVIDER")
        versions: dict[str, Any] = {
            **self._config,
            "application_source_sha256": source_fingerprint(self._root),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "packages": {
                name: importlib.metadata.version(name)
                for name in (
                    "sentence-transformers",
                    "transformers",
                    "torch",
                    "groq",
                    "httpx",
                    "sqlalchemy",
                    "psycopg",
                    "pgvector",
                    "celery",
                    "pypdf",
                    "markdown-it-py",
                )
            },
        }
        prompt = YamlPromptProvider(self._prompt_directory, strict=True).get("grounded_answer", 2)
        versions["prompt_sha256"] = hashlib.sha256(prompt.render().encode()).hexdigest()
        try:
            options: dict[str, Any] = {
                "cwd": self._root,
                "capture_output": True,
                "text": True,
                "check": True,
                "timeout": 5,
            }
            if sys.platform == "win32":
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            versions["application_git_commit"] = subprocess.run(
                ["git", "rev-parse", "HEAD"], **options
            ).stdout.strip()
            versions["application_git_dirty"] = bool(
                subprocess.run(
                    ["git", "status", "--porcelain", "--untracked-files=normal"], **options
                ).stdout.strip()
            )
        except (OSError, subprocess.SubprocessError):
            versions["application_git_commit"] = None
            versions["application_git_dirty"] = None
        model = self._config["embedding"]["model"]
        if "/" not in model:
            model = "sentence-transformers/" + model
        cached = try_to_load_from_cache(model, "config.json")
        versions["embedding_weights_revision"] = (
            Path(cached).parent.name if isinstance(cached, str) else None
        )
        versions["llm_weights_digest"] = None
        if llm["provider"] == "ollama":
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.get(self._ollama_url.rstrip("/") + "/api/tags")
                    response.raise_for_status()
                    name = llm["model"] if ":" in llm["model"] else llm["model"] + ":latest"
                    match = next((m for m in response.json()["models"] if m["name"] == name), None)
                    if match is None:
                        raise EvaluationSetupError("OLLAMA_MODEL_NOT_INSTALLED")
                    versions["llm_weights_digest"] = match["digest"]
            except (httpx.HTTPError, KeyError, ValueError):
                raise EvaluationSetupError("OLLAMA_MODEL_IDENTITY_UNAVAILABLE") from None
        return versions
