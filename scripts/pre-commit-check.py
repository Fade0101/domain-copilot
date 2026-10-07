# Pre-commit quality gate hook.
#
# This script is intended to be installed as a Git pre-commit hook or run
# manually before committing. It enforces the same checks that CI runs,
# catching failures locally before they reach the remote.
#
# Install:  copy to .git/hooks/pre-commit  (or use the install-hooks command)
# Manual:   python scripts/pre-commit-check.py

"""Pre-commit quality gate — runs lint, type-check, architecture contracts, and tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _run(label: str, cmd: list[str]) -> bool:
    """Run a command and report pass/fail."""
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    repo_root = str(Path(__file__).resolve().parent.parent)
    result = subprocess.run(cmd, cwd=repo_root)
    if result.returncode != 0:
        print(f"  ❌ FAILED: {label}")
        return False
    print(f"  ✅ PASSED: {label}")
    return True


def main() -> int:
    """Run all quality gates. Returns 0 on success, 1 on any failure."""
    # Ensure UTF-8 output on Windows consoles
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined, union-attr]

    python = sys.executable
    # lint-imports is a script entry point, not a -m module
    venv_scripts = Path(sys.executable).parent
    lint_imports = str(venv_scripts / "lint-imports")

    checks = [
        ("Ruff lint", [python, "-m", "ruff", "check", "."]),
        ("Ruff format", [python, "-m", "ruff", "format", "--check", "."]),
        ("Mypy type-check", [python, "-m", "mypy", "."]),
        ("Import-linter contracts", [lint_imports]),
        ("Pytest", [python, "-m", "pytest", "--tb=short", "-q"]),
    ]

    results = []
    for label, cmd in checks:
        results.append((label, _run(label, cmd)))

    print(f"\n{'=' * 60}")
    print("  SUMMARY")
    print(f"{'=' * 60}")
    all_passed = True
    for label, passed in results:
        status = "✅" if passed else "❌"
        print(f"  {status} {label}")
        if not passed:
            all_passed = False

    if not all_passed:
        print("\n  ⛔ Pre-commit checks FAILED. Fix issues before committing.\n")
        return 1

    print("\n  🎉 All pre-commit checks passed.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
