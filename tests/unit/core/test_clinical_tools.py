"""Clinical finalization never falls back to the development in-memory stores."""

from pathlib import Path

import pytest

from app.application.errors import ConfigurationError
from app.core.config import DatabaseSettings, LLMSettings, PromptSettings, Settings
from app.core.container import Container


async def test_clinical_tool_factory_requires_authoritative_postgres() -> None:
    root = Path(__file__).resolve().parents[3]
    container = Container(
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            database=DatabaseSettings(url=None),
            llm=LLMSettings(provider="ollama", fallback=None),
            prompts=PromptSettings(directory=str(root / "prompts")),
        )
    )
    try:
        with pytest.raises(ConfigurationError, match="DATABASE__URL"):
            container.clinical_tool_factory()
        with pytest.raises(ConfigurationError, match="DATABASE__URL"):
            from uuid import uuid4

            from tests.support.knowledge_fakes import principal

            container.guideline_researcher_agent(principal(), uuid4())
    finally:
        await container.dispose()
