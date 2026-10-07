"""Informational RAG fallback (BRD AC-5.5, Ticket #17 Part 6).

If the multi-agent research path fails, this fallback surfaces relevant
informational context and citations.

Hard Invariants:
1. Returns informational context and citations ONLY.
2. NEVER produces or finalizes a clinical note.
3. NEVER bypasses Safety Checker or claims safety was established.
4. NEVER transitions the clinical workflow to FINALIZE or COMPLETED.
"""

from __future__ import annotations

from app.application.auth.context import Principal
from app.application.qa.use_cases import AskUseCase
from app.application.workflow.contracts import InformationalFallbackResult


class InformationalRagFallback:
    def __init__(self, ask_use_case: AskUseCase) -> None:
        self._ask = ask_use_case

    async def retrieve_context(
        self, question: str, principal: Principal, *, failure_reason: str = ""
    ) -> InformationalFallbackResult:
        try:
            result = await self._ask.execute(question, principal)
            refusal_reason = failure_reason
            if not refusal_reason and result.refused:
                refusal_reason = f"Refused: {result.answer}"
            return InformationalFallbackResult(
                context=result.answer,
                citations=result.citations,
                refusal_reason=refusal_reason,
                is_informational_only=True,
            )
        except Exception as exc:
            return InformationalFallbackResult(
                context="",
                citations=(),
                refusal_reason=f"Informational fallback failed: {exc}",
                is_informational_only=True,
            )
