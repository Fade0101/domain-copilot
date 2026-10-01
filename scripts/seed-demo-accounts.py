#!/usr/bin/env python
"""seed-demo-accounts — Create the development demo accounts (analyst/reviewer/admin).

Usage:
    AUTH__DEMO_PASSWORD=<password> python scripts/seed-demo-accounts.py

Seeds one account per role in the BRD AC-8.2 matrix so the RBAC behaviour can be
exercised without a registration endpoint. Safe to run repeatedly: existing
accounts are detected by email and left untouched.

DEVELOPMENT ONLY. These are not production credentials:

  * The password comes from AUTH__DEMO_PASSWORD in the environment. There is no
    default and nothing is stored in the repository, so a checkout carries no
    usable credential (constraint C6).
  * Seeding refuses to run when ENVIRONMENT=production.
  * The addresses use example.com (RFC 2606), which cannot reach a real mailbox.

The same seeding runs automatically from the API's startup lifespan hook; this
script is for seeding without booting the API.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Add project root to sys.path so we can import app modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.application.auth.seeding import DEMO_ACCOUNTS
from app.core.container import get_container


async def _seed() -> int:
    container = get_container()
    result = await container.seed_demo_accounts()

    if result is None:
        print("⚠️  Seeding skipped. One of the following applied:")
        print("     - AUTH__SEED_DEMO_ACCOUNTS is false")
        print("     - ENVIRONMENT=production")
        print("     - AUTH__DEMO_PASSWORD is not set")
        print("\n   Set AUTH__DEMO_PASSWORD and re-run to create the demo accounts.")
        return 1

    for spec in DEMO_ACCOUNTS:
        mark = "✅ created" if spec.email in result.created else "➖ exists "
        print(f"  {mark}  {spec.email:24} role={spec.role.value}")

    print(
        f"\n🎉 {len(result.created)} created, {len(result.skipped)} already present "
        f"({result.total} demo accounts)."
    )
    print("   Password: the value of AUTH__DEMO_PASSWORD (not printed).")
    return 0


def main() -> int:
    # Ensure UTF-8 output on Windows consoles
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined, union-attr]

    print("Seeding development demo accounts\n")
    return asyncio.run(_seed())


if __name__ == "__main__":
    sys.exit(main())
