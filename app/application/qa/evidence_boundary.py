"""Conservative instruction detection at the existing extractive answer boundary.

This is a reject-only control, not a clinical Safety Checker or a claim that all
prompt injections are detectable. It never changes source text, prompts, tools,
permissions or workflow state. Unknown attacks still face #10's constrained
decoder and #18's independently enforced capability/approval boundaries.
"""

from __future__ import annotations

import re
import unicodedata

EVIDENCE_BOUNDARY_VERSION = "instruction-data-boundary-v1"

_SIGNALS = (
    (
        "instruction_override",
        re.compile(
            r"\b(?:ignore|disregard|override|bypass|forget)\b.{0,100}"
            r"\b(?:instructions?|rules?|polic(?:y|ies)|constraints?|requirements?|"
            r"previous|evidence|approval|safety)\b"
        ),
    ),
    (
        "role_impersonation",
        re.compile(
            r"\b(?:system|developer|assistant)\s+(?:override|directive|instruction|message|prompt)"
            r"|<\|(?:im_start|start_header_id)\|>|</?(?:system|developer|assistant)>"
            r"|\[(?:system|inst)\]"
        ),
    ),
    (
        "tool_or_routing_override",
        re.compile(
            r"\b(?:finalize_clinical_note|approve_clinical_note|switch_agent|set_approval)\b"
            r"|\b(?:switch|route|become|act as)\b.{0,80}"
            r"\b(?:orchestrator|admin|system|developer|agent|safety checker|researcher)\b"
        ),
    ),
    (
        "approval_forgery",
        re.compile(
            r"\b(?:auto[ -]?approve|skip (?:the )?(?:human )?approval)\b"
            r"|\bapproved[\"']?\s*[:=]\s*(?:true|[\"']approved)\b"
            r"|\bmark\b.{0,60}\bapproved\b"
        ),
    ),
)


def instruction_signals(text: str) -> tuple[str, ...]:
    """Recognize explicit instruction lures, including format-character obfuscation.

    Normalize only for detection; indexed bytes and citation metadata stay exact.
    A match makes the material ineligible for a clinical answer, even if a model
    selects a valid chunk ID. Ordinary clinical imperatives (e.g. "do not use")
    are not policy overrides.
    """
    text = " ".join(
        "".join(
            character
            for character in unicodedata.normalize("NFKC", text).casefold()
            if unicodedata.category(character) != "Cf"
        ).split()
    )
    return tuple(name for name, pattern in _SIGNALS if pattern.search(text))
