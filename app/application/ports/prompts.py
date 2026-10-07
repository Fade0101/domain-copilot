"""Port: versioned prompt provider (BRD AR-4).

Product prompts are versioned artifacts, never inline string literals. The
application depends on the :class:`IPromptProvider` port and the framework-free
:class:`Prompt` value; the concrete loader (which parses YAML) lives in
infrastructure, so ``app.application`` never imports a YAML/SDK library.

Versioning is explicit and deterministic: each prompt has an integer ``version``.
``IPromptProvider.get(prompt_id)`` returns the highest available version;
``get(prompt_id, version=1)`` returns exactly that version (see the loader in
``app.infrastructure.prompts``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.application.errors import PromptValidationError


@dataclass(frozen=True, slots=True)
class Prompt:
    """A single versioned prompt artifact.

    ``template`` uses ``str.format`` placeholders (``{variable}``); the exact set
    of expected placeholders is declared in ``input_variables`` so callers -- and
    tests -- fail loudly on a variable mismatch rather than silently producing a
    malformed prompt.
    """

    id: str
    version: int
    description: str
    input_variables: tuple[str, ...]
    template: str

    def render(self, **values: object) -> str:
        """Render the template, requiring exactly the declared ``input_variables``.

        A missing or unexpected variable, or a placeholder the template
        references but did not declare, raises :class:`PromptValidationError`
        (a configuration fault -- mapped to a safe HTTP 500 at the boundary).
        """
        provided = set(values)
        required = set(self.input_variables)
        if provided != required:
            missing = sorted(required - provided)
            unexpected = sorted(provided - required)
            raise PromptValidationError(
                f"prompt '{self.id}' v{self.version} render mismatch: "
                f"missing={missing} unexpected={unexpected}"
            )
        try:
            return self.template.format(**values)
        except (KeyError, IndexError) as exc:
            raise PromptValidationError(
                f"prompt '{self.id}' v{self.version} references undeclared placeholder {exc}"
            ) from exc


class IPromptProvider(Protocol):
    """Resolves prompts by id and (optionally) exact version."""

    def get(self, prompt_id: str, version: int | str | None = None) -> Prompt:
        """Return a prompt.

        ``version=None`` resolves the highest available version; an explicit
        ``version`` (``int`` or its ``str`` form) resolves that exact version.
        Raises ``PromptNotFoundError`` if no matching prompt exists.
        """
        ...
