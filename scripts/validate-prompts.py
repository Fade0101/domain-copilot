#!/usr/bin/env python
"""validate-prompts — Validate all prompt artifacts in prompts/.

Usage:
    python scripts/validate-prompts.py

Loads every *.yaml file in the prompts/ directory through the
YamlPromptProvider in strict mode and reports any validation errors.
This is the same validation that runs at application startup.

Use this command to catch prompt issues before committing or deploying.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add project root to sys.path so we can import app modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.application.errors import ConfigurationError
from app.infrastructure.prompts.yaml_prompt_provider import YamlPromptProvider


def main() -> int:
    # Ensure UTF-8 output on Windows consoles
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined, union-attr]

    prompts_dir = Path(__file__).resolve().parent.parent / "prompts"

    print(f"Validating prompt artifacts in {prompts_dir}/\n")

    try:
        provider = YamlPromptProvider(prompts_dir, strict=True)
    except ConfigurationError as e:
        print(f"❌ Validation FAILED: {e}")
        return 1

    # List all loaded prompts
    count = 0
    for prompt_id, versions in sorted(provider._by_id.items()):
        for version, prompt in sorted(versions.items()):
            print(f"  ✅ {prompt.id} v{prompt.version} — {prompt.description[:60]}...")
            count += 1

    if count == 0:
        print("  ⚠️  No prompt files found.")
        return 1

    print(f"\n🎉 All {count} prompt artifact(s) validated successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
