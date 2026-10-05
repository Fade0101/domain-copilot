"""Submit, poll and export a real queued evaluation. No local answer simulation.

One command after service setup: python scripts/evaluate.py run --prepare
Credentials: INGEST_ACCESS_TOKEN or AUTH__DEMO_PASSWORD (as in the #8 CLI).
Exit 0: complete run, targets met. 2: measured failures/targets missed.
Exit 1: setup, transport, timeout, incomplete or failed job.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

import httpx

ROOT = Path(__file__).resolve().parents[1]
# Direct execution puts scripts/ on sys.path; keep imports of the existing CLI
# helpers usable both that way and as `python -m scripts.evaluate`.
sys.path.insert(0, str(ROOT))
from scripts.ingest_documents import _json, _token  # noqa: E402


def prepare(api_url: str, timeout: float, directory: Path) -> None:
    command = [
        sys.executable,
        str(ROOT / "scripts/corpus.py"),
        "ingest",
        "--api-url",
        api_url,
        "--timeout",
        str(timeout),
        "--output-directory",
        str(directory / "corpus"),
    ]
    subprocess.run(command, cwd=ROOT, check=True)
    dataset = json.loads((ROOT / "data/evaluation/golden.v1.json").read_text(encoding="utf-8"))
    fixture_directory = directory / "fixtures"
    fixture_directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for fixture in dataset["fixtures"]:
        source = (ROOT / "data/evaluation" / fixture["path"]).resolve()
        if not source.is_relative_to((ROOT / "data/evaluation/fixtures").resolve()):
            raise ValueError("Fixture path escapes its source directory")
        # LF bytes match the catalog pin on Windows and Linux alike.
        destination = fixture_directory / source.name
        destination.write_bytes(source.read_text(encoding="utf-8").encode("utf-8"))
        paths.append(str(destination))
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/ingest_documents.py"),
            "--wait",
            "--api-url",
            api_url,
            "--timeout",
            str(timeout),
            *paths,
        ],
        cwd=ROOT,
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "status", "cancel", "restart", "report"])
    parser.add_argument("job_id", nargs="?", type=UUID)
    parser.add_argument(
        "--api-url", default=os.environ.get("INGEST_API_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="Build and idempotently ingest the pinned corpus and attack fixtures via #8",
    )
    parser.add_argument("--output-directory", type=Path, default=ROOT / ".tasks/evaluation")
    parser.add_argument("--timeout", type=float, default=7200)
    parser.add_argument("--no-wait", action="store_true")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be positive and finite")
    if args.command != "run" and args.job_id is None:
        parser.error("This command requires a job UUID")
    if args.prepare and args.command != "run":
        parser.error("--prepare applies only to run")
    output = args.output_directory.resolve()
    if output.is_relative_to((ROOT / "data").resolve()):
        parser.error("Reports/build artifacts must not overwrite versioned input data")
    try:
        if args.prepare:
            prepare(args.api_url, args.timeout, output)
        with httpx.Client(base_url=args.api_url, timeout=60) as client:
            client.headers["Authorization"] = "Bearer " + _token(client)
            job_id = args.job_id
            if args.command == "run":
                accepted = _json(client.post("/api/v1/evaluations"))
                job_id = UUID(accepted["job_id"])
                print(json.dumps(accepted), flush=True)
            elif args.command in {"cancel", "restart"}:
                accepted = _json(client.post(f"/api/v1/evaluations/{job_id}/{args.command}"))
                job_id = UUID(accepted["job_id"])
                print(json.dumps(accepted), flush=True)
            if args.no_wait or args.command == "cancel":
                return 0
            deadline = time.monotonic() + args.timeout
            previous = None
            while True:
                status = _json(client.get(f"/api/v1/evaluations/{job_id}"))
                progress = (status["state"], status["completed_cases"])
                if progress != previous:
                    print(
                        json.dumps(
                            {
                                k: status[k]
                                for k in (
                                    "job_id",
                                    "state",
                                    "completed_cases",
                                    "total_cases",
                                    "error",
                                )
                            }
                        ),
                        flush=True,
                    )
                    previous = progress
                if args.command == "status":
                    return 0
                if (
                    status["state"] in {"COMPLETED", "CANCELLED", "FAILED"}
                    or args.command == "report"
                ):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"Polling timed out; job {job_id} remains durable. Use status or restart."
                    )
                time.sleep(2)
            report = _json(client.get(f"/api/v1/evaluations/{job_id}/report"))
            markdown = client.get(
                f"/api/v1/evaluations/{job_id}/report", params={"format": "markdown"}
            )
            markdown.raise_for_status()
            output.mkdir(parents=True, exist_ok=True)
            (output / f"{job_id}.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            (output / f"{job_id}.md").write_text(markdown.text, encoding="utf-8")
            print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
            print(f"Reports: {output / str(job_id)}.json / .md", flush=True)
            if report["state"] != "COMPLETED":
                return 1
            return 0 if report["summary"]["targets_met"] else 2
    except (
        httpx.HTTPError,
        OSError,
        ValueError,
        RuntimeError,
        subprocess.CalledProcessError,
    ) as exc:
        # Transport exception messages can embed authenticated URLs. The durable
        # job UUID and safe server-side error code above are the diagnostic path.
        print(f"Evaluation command failed: {type(exc).__name__}.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
