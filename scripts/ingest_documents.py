"""Upload PDF/Markdown sources through the authenticated ingestion API.

Use --seed for two synthetic smoke examples, and --wait to report final status.
Credentials come from INGEST_ACCESS_TOKEN, or AUTH__DEMO_PASSWORD for the demo
admin login. Source text, passwords and tokens are never written to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx


def synthetic_sources() -> list[tuple[str, bytes]]:
    """Smoke inputs, not clinical guidance or Ticket 11 corpus content."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    markdown = (
        b"# Synthetic demonstration guide\n\n"
        b"This file contains no patient information or clinical recommendations.\n\n"
        b"## Evidence review\n\n"
        b"The fictional alpha process checks the source document before recording a summary.\n\n"
        b"## Follow up\n\n"
        b"The fictional beta process records that the demonstration review is complete.\n"
    )
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {
                    NameObject("/F1"): DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Font"),
                            NameObject("/Subtype"): NameObject("/Type1"),
                            NameObject("/BaseFont"): NameObject("/Helvetica"),
                        }
                    )
                }
            ),
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 50 740 Td 18 TL "
        b"(SYNTHETIC DEMONSTRATION) Tj T* "
        b"(This file has no patient information or clinical recommendations.) Tj T* "
        b"(The fictional gamma process checks that evidence is searchable.) Tj T* ET"
    )
    page[NameObject("/Contents")] = stream
    output = BytesIO()
    writer.write(output)
    return [
        ("synthetic-demonstration.md", markdown),
        ("synthetic-demonstration.pdf", output.getvalue()),
    ]


def _json(response: httpx.Response) -> dict[str, Any]:
    if response.is_error:
        raise RuntimeError(f"API request failed with HTTP {response.status_code}.")
    return response.json()


def _token(client: httpx.Client) -> str:
    token = os.environ.get("INGEST_ACCESS_TOKEN", "").strip()
    if token:
        return token
    password = os.environ.get("AUTH__DEMO_PASSWORD", "")
    if not password:
        raise RuntimeError("Set INGEST_ACCESS_TOKEN or AUTH__DEMO_PASSWORD for the demo admin.")
    body = _json(
        client.post("/api/v1/auth/token", json={"email": "admin@example.com", "password": password})
    )
    return str(body["access_token"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", type=Path)
    parser.add_argument(
        "--seed", action="store_true", help="Ingest two synthetic PDF/Markdown examples"
    )
    parser.add_argument(
        "--wait", action="store_true", help="Poll each document until it completes or fails"
    )
    parser.add_argument(
        "--api-url", default=os.environ.get("INGEST_API_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument("--version", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=900)
    args = parser.parse_args()
    if not args.files and not args.seed:
        parser.error("Supply PDF/Markdown paths or --seed.")
    failed = False
    try:
        sources = synthetic_sources() if args.seed else []
        for path in args.files:
            if path.stat().st_size > 104_857_600:
                raise RuntimeError("A source exceeds the maximum supported upload size.")
            sources.append((path.name, path.read_bytes()))
        with httpx.Client(base_url=args.api_url, timeout=60) as client:
            headers = {"Authorization": "Bearer " + _token(client)}
            for filename, source in sources:
                media_type = (
                    "application/pdf" if filename.lower().endswith(".pdf") else "text/markdown"
                )
                accepted = _json(
                    client.post(
                        "/api/v1/documents/ingest",
                        params={"filename": filename, "version": args.version},
                        content=source,
                        headers={**headers, "Content-Type": media_type},
                    )
                )
                print(json.dumps({"filename": filename, **accepted}), flush=True)
                if not args.wait:
                    continue
                deadline = time.monotonic() + args.timeout
                while True:
                    job = _json(client.get(accepted["status_url"], headers=headers))
                    document = _json(client.get(accepted["document_url"], headers=headers))
                    if job["state"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                        failed |= job["state"] != "COMPLETED"
                        print(
                            json.dumps(
                                {
                                    "filename": filename,
                                    "job_id": accepted["job_id"],
                                    "status": document["status"],
                                    "chunk_count": document["chunk_count"],
                                    "error_stage": document["error_stage"],
                                    "error": document["error_message"],
                                }
                            ),
                            flush=True,
                        )
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "Timed out waiting for ingestion; the job remains queryable."
                        )
                    time.sleep(1)
    except (OSError, RuntimeError, httpx.HTTPError) as exc:
        print(f"Ingestion failed: {exc}", file=sys.stderr)
        return 1
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
