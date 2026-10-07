"""Conservative evidence checks and constrained, extractive answer decoding.

The model selects existing chunks. It cannot write clinical prose or citation
metadata. Whole chunks are quoted so qualifiers and negations remain intact.
These are Q&A grounding rules, not the separate clinical-note Safety Checker.
"""

from __future__ import annotations

import json
import re
from uuid import UUID

from app.application.retrieval.dto import RankedCandidate

REFUSAL = "Not enough information in the corpus"

_DOSE_QUERY = re.compile(r"\b(dos(?:e|es|age|ing)|how much|milligrams?|mg|mcg|titration)\b", re.I)
_CONTRA_QUERY = re.compile(
    r"\b(contraindicat\w*|avoid|unsafe|safe|should not|must not|can(?:not|'t)|pregnan\w*)\b", re.I
)
_INTERACTION_QUERY = re.compile(
    r"\b(interact\w*|combin\w*|together|concomitant\w*|co[ -]?administ\w*)\b", re.I
)
_DOSE_VALUE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|µg|ug|g|ml|iu|units?|milligrams?|micrograms?)\b", re.I
)
_DOSE_CONTEXT = re.compile(r"\b(dos\w*|administ\w*|take|given|daily|once|twice|every)\b", re.I)
_CONTRA_STATEMENT = re.compile(
    r"\b(contraindicat\w*|do not (?:use|take|give)|must not|avoid|not recommended)\b", re.I
)
_INTERACTION_STATEMENT = re.compile(
    r"\b(interact\w*|co[ -]?administ\w*|concomitant\w*|combination|together)\b", re.I
)
_UNSPECIFIED = re.compile(
    r"\b(unknown|not (?:known|studied|specified|provided|established|available)|"
    r"no (?:available |documented )?(?:data|information|evidence))\b",
    re.I,
)


def has_explicit_clinical_evidence(query: str, candidates: tuple[RankedCandidate, ...]) -> bool:
    """Require explicit source statements for high-risk question categories.

    Presence checks can only reject, never establish clinical correctness. The
    generation prompt still must assess relevance, population and contradictions.
    Every accepted answer remains a verbatim source quotation.
    """
    sentences = [
        sentence
        for candidate in candidates
        for sentence in re.split(r"(?<=[.!?])\s+|\n", candidate.hit.snippet)
        if not _UNSPECIFIED.search(sentence)
    ]
    if _DOSE_QUERY.search(query) and not any(
        _DOSE_VALUE.search(sentence) and _DOSE_CONTEXT.search(sentence) for sentence in sentences
    ):
        return False
    if _CONTRA_QUERY.search(query) and not any(_CONTRA_STATEMENT.search(s) for s in sentences):
        return False
    if _INTERACTION_QUERY.search(query) and not any(
        _INTERACTION_STATEMENT.search(s) for s in sentences
    ):
        return False
    return True


def has_direct_conflict(candidates: tuple[RankedCandidate, ...]) -> bool:
    """Reject literal contradictory statements without asking a model to choose.

    Same wording but different numbers, or the addition/removal of negation,
    is ambiguous evidence. More nuanced contextual conflicts are judged in the
    grounded-answer prompt; this deliberately does not infer clinical semantics.
    """
    statements: dict[str, set[str]] = {}
    for candidate in candidates:
        for sentence in re.split(r"(?<=[.!?])\s+|\n", candidate.hit.snippet):
            normalized = " ".join(sentence.casefold().split())
            if not normalized:
                continue
            skeleton = re.sub(r"\b\d+(?:\.\d+)?\b", "<number>", normalized)
            skeleton = re.sub(r"\b(?:not|never|no)\s+", "", skeleton)
            variants = statements.setdefault(skeleton, set())
            variants.add(normalized)
            if len(variants) > 1:
                return True
    return False


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("Duplicate output field")
        values[key] = value
    return values


def decode_evidence_selection(
    content: str | None, candidates: tuple[RankedCandidate, ...]
) -> tuple[RankedCandidate, ...]:
    """Accept only selected source IDs; malformed/unsupported output refuses.

    The strict shape excludes model-written answers, fabricated citations and
    invented metadata, even if the rest of the response appears well formed.
    """
    if content is None or len(content) > 16_000:
        return ()
    try:
        payload = json.loads(content, object_pairs_hook=_unique_object)
        if not isinstance(payload, dict) or set(payload) != {"status", "chunk_ids"}:
            return ()
        if payload["status"] != "supported":
            return ()
        ids = payload["chunk_ids"]
        if not isinstance(ids, list) or not ids or len(ids) > len(candidates):
            return ()
        if not all(isinstance(value, str) for value in ids):
            return ()
        identifiers = [UUID(value) for value in ids]
        if len(set(identifiers)) != len(identifiers):
            return ()
        available = {candidate.hit.chunk_id: candidate for candidate in candidates}
        if any(identifier not in available for identifier in identifiers):
            return ()
        # Keep the deterministic reranker order, regardless of LLM list order.
        chosen = set(identifiers)
        return tuple(candidate for candidate in candidates if candidate.hit.chunk_id in chosen)
    except (ValueError, TypeError):
        return ()
