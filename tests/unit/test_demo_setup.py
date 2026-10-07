from pathlib import Path

from dotenv import dotenv_values

from scripts.configure_demo import create_demo_env


def test_demo_configuration_generates_matching_private_credentials(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / ".env.example"
    (tmp_path / ".env.example").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    assert create_demo_env(tmp_path)
    settings = dotenv_values(tmp_path / ".env")
    password = settings["POSTGRES_PASSWORD"]
    assert password and len(password) >= 24
    assert settings["DATABASE__URL"] == (
        f"postgresql://postgres:{password}@postgres:5432/domain_copilot"
    )
    assert len(settings["AUTH__SECRET_KEY"] or "") >= 32
    assert len(settings["AUTH__DEMO_PASSWORD"] or "") >= 12
    assert len({password, settings["AUTH__SECRET_KEY"], settings["AUTH__DEMO_PASSWORD"]}) == 3
    assert settings["LLM__FALLBACK"] == ""
    assert settings["LLM__MODEL"] == "openai/gpt-oss-20b"
    assert not settings["LLM__API_KEY"]
    assert settings["ENVIRONMENT"] == "development"


def test_demo_configuration_preserves_existing_file(tmp_path: Path) -> None:
    target = tmp_path / ".env"
    original = b"# Existing operator configuration\nDATABASE__URL=existing\n"
    target.write_bytes(original)
    assert not create_demo_env(tmp_path)
    assert target.read_bytes() == original
