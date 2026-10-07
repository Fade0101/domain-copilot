"""Explicit operations: python -m app.core.jobs_cli reconcile | resume JOB_ID."""

from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from app.application.errors import ApplicationError
from app.core.container import get_job_runtime
from app.domain.shared.errors import DomainError


async def _execute(args: argparse.Namespace) -> int:
    runtime = get_job_runtime()
    try:
        if args.command == "reconcile":
            count = await runtime.service.reconcile(limit=args.limit)
            print(f"Published {count} durable pending/queued jobs.")
        else:
            await runtime.service.resume(args.job_id)
            print(f"Published resume for {args.job_id}.")
        return 0
    except (ApplicationError, DomainError) as exc:
        print(f"Job command failed: {type(exc).__name__}.")
        return 1
    finally:
        await runtime.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    reconcile = commands.add_parser("reconcile", help="Republish PENDING and QUEUED jobs only")
    reconcile.add_argument("--limit", type=int, default=100)
    resume = commands.add_parser(
        "resume", help="Resume an operator-confirmed interrupted STARTED job"
    )
    resume.add_argument("job_id", type=UUID)
    return asyncio.run(_execute(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
