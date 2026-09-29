#!/usr/bin/env python
"""check-boundaries — Run architecture boundary checks independently.

Usage:
    python scripts/check-boundaries.py

Runs both the import-linter declarative contracts (from pyproject.toml) and
the AST-based boundary tests (tests/architecture/test_boundaries.py) in a
single command. This is the quick way to verify that a code change has not
violated the Clean Hexagonal Architecture layer rules.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    # Ensure UTF-8 output on Windows consoles
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined, union-attr]

    print("=" * 60)
    print("  Architecture Boundary Check")
    print("=" * 60)

    repo_root = str(Path(__file__).resolve().parent.parent)
    python = sys.executable

    # lint-imports is a script entry point, not a -m module
    venv_scripts = Path(sys.executable).parent
    lint_imports = str(venv_scripts / "lint-imports")

    checks = [
        ("Import-linter contracts", [lint_imports]),
        ("AST boundary tests", [python, "-m", "pytest", "tests/architecture/", "-v", "--tb=short"]),
    ]

    all_passed = True
    for label, cmd in checks:
        print(f"\n--- {label} ---")
        result = subprocess.run(cmd, cwd=repo_root)
        if result.returncode != 0:
            print(f"  ❌ {label} FAILED")
            all_passed = False
        else:
            print(f"  ✅ {label} passed")

    if not all_passed:
        print("\n⛔ Boundary checks FAILED.\n")
        return 1

    print("\n🎉 All architecture boundaries intact.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
