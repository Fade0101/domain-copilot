"""Architecture boundary tests (BRD AR-1; SYSTEM-DESIGN A.3.2).

A dependency-free AST scan that fails if an inner layer imports something it must
not. This complements the declarative import-linter contracts (run as
``lint-imports`` in CI) with a human-readable check that runs in the normal
pytest step. There is intentionally no "explicitly justified exception" escape
hatch for domain/application: SDK/framework imports there are forbidden outright.
"""

from __future__ import annotations

import ast
from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "app"

# Third-party packages that inner layers (domain/application) must never import.
_FORBIDDEN_THIRD_PARTY = {
    "fastapi",
    "starlette",
    "uvicorn",
    "sqlalchemy",
    "alembic",
    "psycopg",
    "pgvector",
    "redis",
    "celery",
    "groq",
    "openai",
    "ollama",
    "sentence_transformers",
    "presidio",
    "httpx",
    "pydantic",
    "pydantic_settings",
    # Auth implementation libraries: reachable only through IPasswordHasher and
    # ITokenService. `passlib` is listed although it is no longer a dependency,
    # so reintroducing it in an inner layer still fails.
    "bcrypt",
    "jwt",
    "passlib",
    "yaml",
}


def _py_files(package: str) -> list[Path]:
    return sorted((_APP / package).rglob("*.py"))


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def _is_within(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)


def _inner_layer_violations(package: str, allowed_app_prefixes: tuple[str, ...]) -> list[str]:
    problems: list[str] = []
    for path in _py_files(package):
        rel = path.relative_to(_APP)
        for module in sorted(_imported_modules(path)):
            if module == "app" or module.startswith("app."):
                if not _is_within(module, allowed_app_prefixes):
                    problems.append(f"{rel}: forbidden layer import '{module}'")
            elif module.split(".")[0] in _FORBIDDEN_THIRD_PARTY:
                problems.append(f"{rel}: forbidden dependency '{module}'")
    return problems


def test_domain_imports_only_stdlib_and_domain() -> None:
    problems = _inner_layer_violations("domain", allowed_app_prefixes=("app.domain",))
    assert not problems, "domain boundary violations:\n" + "\n".join(problems)


def test_application_imports_only_domain_and_application() -> None:
    problems = _inner_layer_violations(
        "application", allowed_app_prefixes=("app.domain", "app.application")
    )
    assert not problems, "application boundary violations:\n" + "\n".join(problems)


def test_presentation_does_not_import_infrastructure() -> None:
    problems: list[str] = []
    for path in _py_files("presentation"):
        for module in sorted(_imported_modules(path)):
            if _is_within(module, ("app.infrastructure",)):
                problems.append(f"{path.relative_to(_APP)}: imports '{module}'")
    assert not problems, "presentation must not import infrastructure:\n" + "\n".join(problems)
