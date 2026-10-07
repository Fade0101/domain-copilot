"""Create a private development .env without installing Python on the host.

Run through the README's Python Docker command. Existing configuration is never
overwritten, and generated credentials are never printed or built into images.
"""

from __future__ import annotations

import os
import re
import secrets
import sys
from pathlib import Path


def create_demo_env(directory: Path) -> bool:
    target = directory / ".env"
    if target.exists():
        return False
    password = secrets.token_urlsafe(24)
    values = {
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": password,
        "POSTGRES_DB": "domain_copilot",
        "DATABASE__URL": f"postgresql://postgres:{password}@postgres:5432/domain_copilot",
        "ENVIRONMENT": "development",
        "AUTH__SECRET_KEY": secrets.token_urlsafe(48),
        "AUTH__DEMO_PASSWORD": secrets.token_urlsafe(18),
        "AUTH__SEED_DEMO_ACCOUNTS": "true",
        "LLM__PROVIDER": "groq",
        "LLM__FALLBACK": "",
    }
    content = (directory / ".env.example").read_text(encoding="utf-8")
    for key, value in values.items():
        content, count = re.subn(rf"(?m)^{key}=.*$", lambda _: f"{key}={value}", content)
        if count != 1:
            raise ValueError(f"Expected exactly one {key} setting in .env.example")
    try:
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        # Docker runs as root by default. Give the generated file back to the
        # checkout owner so a Linux reviewer can edit it without sudo.
        if sys.platform != "win32":
            if os.geteuid() == 0:
                owner = (directory / ".env.example").stat()
                os.chown(target, owner.st_uid, owner.st_gid)
        target.chmod(0o600)
    except FileExistsError:
        return False
    return True


if __name__ == "__main__":
    created = create_demo_env(Path(__file__).resolve().parents[1])
    print("Created private .env." if created else "Kept existing .env unchanged.")
    print("Set LLM__API_KEY in .env to your Groq API key before using chat.")
    print("The three demo accounts use AUTH__DEMO_PASSWORD from that file.")
