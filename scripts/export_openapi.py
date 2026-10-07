"""Publish the default HTTP contract without startup, database access or model loading."""

import argparse
import json
from pathlib import Path

from app.core.config import Settings
from app.presentation.api.app import create_app

OUTPUT = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"


def rendered_openapi() -> str:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]  # BaseSettings runtime option.
        app_name="Domain Copilot",
        api_v1_str="/api/v1",
    )
    app = create_app(settings)
    return json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Fail if the published contract drifted."
    )
    args = parser.parse_args()
    rendered = rendered_openapi()
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != rendered:
            print("OpenAPI is stale. Run python -m scripts.export_openapi")
            return 1
        print("Published OpenAPI matches the runtime routes.")
    else:
        OUTPUT.write_text(rendered, encoding="utf-8")
        print(f"Published {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
