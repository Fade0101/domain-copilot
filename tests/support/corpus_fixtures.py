"""Build the committed corpus offline once for file and ingestion contract tests."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scripts.corpus import DEFAULT_MANIFEST, build, load_manifest


@dataclass(frozen=True)
class BuiltCorpus:
    manifest: dict[str, Any]
    source_directory: Path
    directory: Path
    summary: dict[str, Any]


@pytest.fixture(scope="session")
def built_corpus(tmp_path_factory: pytest.TempPathFactory) -> BuiltCorpus:
    manifest = load_manifest()
    directory = tmp_path_factory.mktemp("healthcare-corpus")
    summary = build(manifest, DEFAULT_MANIFEST.parent, directory)
    return BuiltCorpus(manifest, DEFAULT_MANIFEST.parent, directory, summary)
