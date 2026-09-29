"""YAML-backed prompt provider (infrastructure adapter for AR-4).

Loads versioned prompt artifacts from a directory of ``*.yaml`` files. This is
the *only* layer permitted to import a YAML parser: the application depends on
:class:`~app.application.ports.prompts.IPromptProvider`, never on ``yaml``.

Each artifact declares its own ``id`` and ``version`` (the filename, by
convention ``<id>.v<version>.yaml``, is not authoritative). Prompts are indexed
by ``(id, version)``; ``get`` without a version resolves the numerically highest
version deterministically. When constructed with ``strict=True`` the whole
directory is validated eagerly at startup, so a malformed prompt fails fast at
composition time rather than on first use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.application.errors import PromptNotFoundError, PromptValidationError
from app.application.ports.prompts import IPromptProvider, Prompt

_REQUIRED_KEYS = {"id", "version", "description", "input_variables", "template"}


class YamlPromptProvider(IPromptProvider):
    """Load and resolve versioned prompts from a directory of YAML files."""

    def __init__(self, directory: str | Path, *, strict: bool = True) -> None:
        self._directory = Path(directory)
        # Index: prompt id -> {version -> Prompt}.
        self._by_id: dict[str, dict[int, Prompt]] = {}
        if strict:
            self._load_all()

    def _load_all(self) -> None:
        if not self._directory.is_dir():
            raise PromptValidationError(f"prompt directory not found: {self._directory}")
        for path in sorted(self._directory.glob("*.yaml")):
            prompt = self._parse(path)
            versions = self._by_id.setdefault(prompt.id, {})
            if prompt.version in versions:
                raise PromptValidationError(
                    f"duplicate prompt '{prompt.id}' v{prompt.version} "
                    f"(second definition in {path.name})"
                )
            versions[prompt.version] = prompt

    def _parse(self, path: Path) -> Prompt:
        try:
            raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise PromptValidationError(f"invalid YAML in {path.name}: {exc}") from exc
        if not isinstance(raw, dict):
            raise PromptValidationError(f"prompt {path.name} must be a mapping")

        missing = _REQUIRED_KEYS - raw.keys()
        if missing:
            raise PromptValidationError(f"prompt {path.name} missing keys: {sorted(missing)}")

        version = raw["version"]
        if not isinstance(version, int) or isinstance(version, bool):
            raise PromptValidationError(f"prompt {path.name} 'version' must be an integer")

        template = raw["template"]
        if not isinstance(template, str) or not template.strip():
            raise PromptValidationError(f"prompt {path.name} 'template' must be a non-empty string")

        variables = raw["input_variables"] or []
        if not isinstance(variables, list) or not all(isinstance(v, str) for v in variables):
            raise PromptValidationError(
                f"prompt {path.name} 'input_variables' must be a list of strings"
            )

        return Prompt(
            id=str(raw["id"]),
            version=version,
            description=str(raw["description"]),
            input_variables=tuple(variables),
            template=template,
        )

    def get(self, prompt_id: str, version: int | str | None = None) -> Prompt:
        versions = self._by_id.get(prompt_id)
        if not versions:
            raise PromptNotFoundError(f"unknown prompt '{prompt_id}'")
        if version is None:
            latest = max(versions)
            return versions[latest]
        try:
            wanted = int(version)
        except (TypeError, ValueError) as exc:
            raise PromptNotFoundError(
                f"invalid version '{version}' for prompt '{prompt_id}'"
            ) from exc
        if wanted not in versions:
            raise PromptNotFoundError(f"prompt '{prompt_id}' has no version {wanted}")
        return versions[wanted]
