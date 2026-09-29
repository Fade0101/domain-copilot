"""Unit tests for the YAML prompt provider and Prompt rendering (BRD AR-4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.application.errors import PromptNotFoundError, PromptValidationError
from app.application.ports.prompts import Prompt
from app.infrastructure.prompts.yaml_prompt_provider import YamlPromptProvider

_REAL_PROMPTS_DIR = Path(__file__).resolve().parents[3] / "prompts"


def _write(dir_: Path, name: str, body: str) -> None:
    (dir_ / name).write_text(body, encoding="utf-8")


def _valid_yaml(prompt_id: str = "greeting", version: int = 1) -> str:
    return (
        f"id: {prompt_id}\n"
        f"version: {version}\n"
        "description: A greeting.\n"
        "input_variables:\n"
        "  - name\n"
        "template: |\n"
        "  Hello {name}.\n"
    )


def test_loads_valid_prompt(tmp_path: Path) -> None:
    _write(tmp_path, "greeting.v1.yaml", _valid_yaml())
    provider = YamlPromptProvider(tmp_path)

    prompt = provider.get("greeting")

    assert isinstance(prompt, Prompt)
    assert prompt.id == "greeting"
    assert prompt.version == 1
    assert prompt.input_variables == ("name",)
    assert "Hello {name}." in prompt.template


def test_unknown_prompt_raises_not_found(tmp_path: Path) -> None:
    _write(tmp_path, "greeting.v1.yaml", _valid_yaml())
    provider = YamlPromptProvider(tmp_path)

    with pytest.raises(PromptNotFoundError):
        provider.get("does-not-exist")


def test_unknown_version_raises_not_found(tmp_path: Path) -> None:
    _write(tmp_path, "greeting.v1.yaml", _valid_yaml())
    provider = YamlPromptProvider(tmp_path)

    with pytest.raises(PromptNotFoundError):
        provider.get("greeting", version=99)


def test_missing_key_raises_validation_error(tmp_path: Path) -> None:
    _write(tmp_path, "broken.v1.yaml", "id: broken\nversion: 1\ntemplate: hi\n")

    with pytest.raises(PromptValidationError):
        YamlPromptProvider(tmp_path)


def test_empty_template_raises_validation_error(tmp_path: Path) -> None:
    body = "id: blank\nversion: 1\ndescription: blank\ninput_variables: []\ntemplate: '   '\n"
    _write(tmp_path, "blank.v1.yaml", body)

    with pytest.raises(PromptValidationError):
        YamlPromptProvider(tmp_path)


def test_non_integer_version_raises_validation_error(tmp_path: Path) -> None:
    _write(tmp_path, "greeting.v1.yaml", _valid_yaml().replace("version: 1", "version: one"))

    with pytest.raises(PromptValidationError):
        YamlPromptProvider(tmp_path)


def test_latest_version_is_resolved_deterministically(tmp_path: Path) -> None:
    _write(tmp_path, "greeting.v1.yaml", _valid_yaml(version=1))
    _write(tmp_path, "greeting.v2.yaml", _valid_yaml(version=2))
    _write(tmp_path, "greeting.v3.yaml", _valid_yaml(version=3))
    provider = YamlPromptProvider(tmp_path)

    assert provider.get("greeting").version == 3
    assert provider.get("greeting", version=1).version == 1
    assert provider.get("greeting", version="2").version == 2


def test_duplicate_version_raises_validation_error(tmp_path: Path) -> None:
    _write(tmp_path, "greeting.v1.yaml", _valid_yaml(version=1))
    _write(tmp_path, "greeting-copy.yaml", _valid_yaml(version=1))

    with pytest.raises(PromptValidationError):
        YamlPromptProvider(tmp_path)


def test_missing_directory_raises_validation_error(tmp_path: Path) -> None:
    with pytest.raises(PromptValidationError):
        YamlPromptProvider(tmp_path / "nope")


def test_render_fills_declared_variables() -> None:
    prompt = Prompt(
        id="greeting",
        version=1,
        description="A greeting.",
        input_variables=("name",),
        template="Hello {name}.",
    )

    assert prompt.render(name="Ada") == "Hello Ada."


def test_render_rejects_missing_variable() -> None:
    prompt = Prompt(
        id="greeting",
        version=1,
        description="A greeting.",
        input_variables=("name",),
        template="Hello {name}.",
    )

    with pytest.raises(PromptValidationError):
        prompt.render()


def test_shipped_prompts_load() -> None:
    """The real prompts/ directory must always load cleanly (guards shipped YAML)."""
    provider = YamlPromptProvider(_REAL_PROMPTS_DIR)

    assert provider.get("grounded_answer").version >= 1
    assert provider.get("safety_check").version >= 1
