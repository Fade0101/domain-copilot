"""Documentation Drafter Agent (AGT-03).

Composes a reviewable clinical note draft from safety-vetted findings using exactly
one non-side-effecting clinical tool: draft_clinical_note.
Pure standard-library application logic; clean architecture boundary.
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import UUID

from app.application.agents.contracts import (
    CaseSummary,
    ClinicalNoteDraft,
    DeferredClaim,
    ExcludedClaim,
    SafetyClaimCheck,
    SafetyClaimStatus,
    SafetyStatus,
    SafetyVerdict,
    canonical_claim_key,
    partition_verdict_claims,
    serialize_supported_claims,
)
from app.application.agents.evidence import VerifiedEvidenceRegistry
from app.application.clinical_tools.contracts import ToolName, draft_digest
from app.application.clinical_tools.execution import ClinicalToolExecutor
from app.application.ports.audit import AuditEntry
from app.application.ports.llm import (
    CompletionRequest,
    CompletionResponse,
    ILLMProvider,
    ModelOptions,
    ToolCall,
)
from app.application.ports.prompts import IPromptProvider
from app.application.qa.evidence_boundary import instruction_signals
from app.application.qa.grounding import REFUSAL
from app.application.retrieval.dto import Citation
from app.application.retrieval.observability import RetrievalObserver

ALLOWED_TOOLS = frozenset({ToolName.DRAFT_CLINICAL_NOTE.value})
DEFAULT_MAX_ITERATIONS = 4
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_PROMPT_VERSION = 1


class DocumentationDrafterAgent:
    """Documentation Drafter Agent (AGT-03).

    Composes a reviewable clinical note from safety-vetted findings using
    strictly scoped draft_clinical_note tool. Enforces fail-closed safety
    boundaries, claim-local provenance, and review-only draft requirements.
    """

    def __init__(
        self,
        llm: ILLMProvider,
        tools: ClinicalToolExecutor,
        prompts: IPromptProvider,
        observer: RetrievalObserver | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        prompt_version: int = DEFAULT_PROMPT_VERSION,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._prompts = prompts
        self._observer = observer
        self._max_iterations = max_iterations
        self._timeout_seconds = timeout_seconds
        self._prompt_version = prompt_version

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(sorted(ALLOWED_TOOLS))

    def tools(self) -> ClinicalToolExecutor:
        return self._tools

    async def execute(self, verdict: SafetyVerdict, case: CaseSummary) -> ClinicalNoteDraft:
        """Compose a reviewable clinical note draft from safety-vetted findings."""
        if not isinstance(verdict, SafetyVerdict):
            raise TypeError("verdict must be a SafetyVerdict")
        if not isinstance(case, CaseSummary):
            raise TypeError("case must be a CaseSummary")
        if verdict.workflow_id != case.workflow_id:
            raise ValueError("Workflow ID mismatch between verdict and case")

        # 1. Fail-closed check on safety verdict failure or refused empty verdict
        if verdict.status in (SafetyStatus.TOOL_ERROR, SafetyStatus.TIMEOUT) or (
            verdict.refused and not verdict.checked_claims
        ):
            return ClinicalNoteDraft(
                workflow_id=verdict.workflow_id,
                draft_id=draft_digest(REFUSAL),
                note=REFUSAL,
                asserted_claims=(),
                excluded_claims=(),
                deferred_claims=(),
                citations=(),
                refused=True,
                safety_status=verdict.status,
                can_proceed=False,
                requires_review=True,
                evidence_trace_ids=(),
                metadata={"refusal_reason": f"Input safety verdict failed with {verdict.status.value}"},
            )

        # 2. Deterministic claim partitioning
        verified_safe, excluded = partition_verdict_claims(verdict)

        # 3. If zero verified safe claims exist, refuse immediately
        if not verified_safe:
            return ClinicalNoteDraft(
                workflow_id=verdict.workflow_id,
                draft_id=draft_digest(REFUSAL),
                note=REFUSAL,
                asserted_claims=(),
                excluded_claims=excluded,
                deferred_claims=(),
                citations=(),
                refused=True,
                safety_status=verdict.status,
                can_proceed=verdict.can_proceed,
                requires_review=True,
                evidence_trace_ids=(),
                metadata={"refusal_reason": "Zero verified safe clinical claims available for drafting."},
            )

        # 4. Fail-closed context serialization (prevent mid-sentence truncation)
        supported_context = serialize_supported_claims(verified_safe)
        if supported_context is None:
            # Complete verified claims exceed tool capacity (4,000 characters)
            deferred = tuple(
                DeferredClaim(
                    claim_id=canonical_claim_key(c.claim_type, c.target, c.detail),
                    claim=f"{c.claim_type.value}: {c.target}",
                    claim_type=c.claim_type,
                    status=SafetyClaimStatus.VERIFIED_SAFE,
                    reason=(
                        "Complete supported context exceeded tool capacity (4000 chars); "
                        "deferred without truncation."
                    ),
                    citations=c.citations,
                )
                for c in verified_safe
            )
            return ClinicalNoteDraft(
                workflow_id=verdict.workflow_id,
                draft_id=draft_digest(REFUSAL),
                note=REFUSAL,
                asserted_claims=(),
                excluded_claims=excluded,
                deferred_claims=deferred,
                citations=(),
                refused=True,
                safety_status=verdict.status,
                can_proceed=verdict.can_proceed,
                requires_review=True,
                evidence_trace_ids=(),
                metadata={
                    "refusal_reason": (
                        "Supported context exceeded tool capacity (4000 chars); "
                        "refused to truncate clinical text."
                    )
                },
            )

        # 5. Agent drafting loop
        deadline = time.monotonic() + self._timeout_seconds
        registry = VerifiedEvidenceRegistry()
        evidence_trace_ids: list[UUID] = []
        quarantined_chunks_count = verdict.quarantined_chunks_count
        iterations = 0

        prompt = self._prompts.get("documentation_drafter", version=self._prompt_version)
        rendered_prompt = prompt.render()

        user_content = json.dumps(
            {
                "workflow_id": str(verdict.workflow_id),
                "clinical_question": case.clinical_question,
                "verified_safe_findings": supported_context,
                "excluded_claims_count": len(excluded),
            },
            ensure_ascii=False,
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": rendered_prompt},
            {"role": "user", "content": user_content},
        ]

        tool_definitions = self._tools.definitions()

        while iterations < self._max_iterations:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                break

            iterations += 1

            request = CompletionRequest(
                messages=list(messages),
                tools=tool_definitions,
                model_options=ModelOptions(
                    temperature=0.0,
                    timeout_seconds=remaining,
                ),
            )

            try:
                response: CompletionResponse = await self._llm.complete(request)
            except Exception:
                break

            if time.monotonic() >= deadline:
                break

            if response.tool_calls:
                for call in response.tool_calls:
                    # Defense-in-depth tool-call validation
                    if call.name not in ALLOWED_TOOLS:
                        if self._observer:
                            self._observer.emit(
                                AuditEntry(
                                    action="unauthorized_tool_rejected",
                                    actor_id="documentation_drafter",
                                    detail={
                                        "attempted_tool": call.name,
                                        "workflow_id": str(verdict.workflow_id),
                                    },
                                )
                            )
                        error_payload = {
                            "ok": False,
                            "tool": call.name,
                            "error": {
                                "code": "PERMISSION_DENIED",
                                "message": (
                                    f"Tool '{call.name}' is outside Documentation Drafter scope. "
                                    f"Available tools are: {sorted(ALLOWED_TOOLS)}"
                                ),
                            },
                        }
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": json.dumps(error_payload),
                            }
                        )
                        continue

                    # Trust boundary enforcement: ensure only safe clinical context is passed
                    enforced_arguments = {
                        "clinical_question": case.clinical_question,
                        "case_summary": supported_context,
                    }
                    safe_call = ToolCall(
                        id=call.id,
                        name=call.name,
                        arguments=json.dumps(enforced_arguments),
                    )

                    tool_result = await self._tools.execute(safe_call)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": tool_result.output,
                        }
                    )

                    try:
                        payload = json.loads(tool_result.output)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    if not payload.get("ok"):
                        continue

                    inner = payload.get("result", {})
                    trace_str = payload.get("trace_id")
                    if trace_str:
                        try:
                            evidence_trace_ids.append(UUID(str(trace_str)))
                        except ValueError:
                            pass

                    # Extract citations from tool execution
                    raw_citations = inner.get("citations", [])
                    clean_tool_citations: list[Citation] = []
                    for cit in raw_citations:
                        try:
                            citation = Citation(
                                document_id=UUID(str(cit["document_id"])),
                                document_name=str(cit["document_name"]),
                                section=cit.get("section"),
                                page=cit.get("page"),
                                chunk_id=UUID(str(cit["chunk_id"])),
                                relevance_score=float(cit["relevance_score"]),
                                text_snippet=str(cit["text_snippet"]),
                            )
                        except (KeyError, ValueError, TypeError):
                            continue

                        # Check instruction signals & quarantine hostile evidence
                        signals = instruction_signals(citation.text_snippet)
                        if signals:
                            quarantined_chunks_count += 1
                        else:
                            registry.register(citation)
                            clean_tool_citations.append(citation)

                    tool_refused = bool(inner.get("refused", True))
                    # Compliance with #18 refused == (not citations) invariant
                    if tool_refused or not clean_tool_citations:
                        return ClinicalNoteDraft(
                            workflow_id=verdict.workflow_id,
                            draft_id=draft_digest(REFUSAL),
                            note=REFUSAL,
                            asserted_claims=(),
                            excluded_claims=excluded,
                            deferred_claims=(),
                            citations=(),
                            refused=True,
                            safety_status=verdict.status,
                            can_proceed=verdict.can_proceed,
                            requires_review=True,
                            evidence_trace_ids=tuple(evidence_trace_ids),
                            quarantined_chunks_count=quarantined_chunks_count,
                            metadata={"refusal_reason": "Draft tool returned refusal or empty citations."},
                        )

                    raw_note = str(inner.get("note", ""))
                    draft_id = str(inner.get("draft_id", draft_digest(raw_note)))

                    # Successfully generated draft
                    return ClinicalNoteDraft(
                        workflow_id=verdict.workflow_id,
                        draft_id=draft_id,
                        note=raw_note,
                        asserted_claims=verified_safe,
                        excluded_claims=excluded,
                        deferred_claims=(),
                        citations=tuple(clean_tool_citations),
                        refused=False,
                        safety_status=verdict.status,
                        can_proceed=verdict.can_proceed,
                        requires_review=True,
                        evidence_trace_ids=tuple(evidence_trace_ids),
                        quarantined_chunks_count=quarantined_chunks_count,
                        metadata={
                            "iterations": iterations,
                            "prompt_version": self._prompt_version,
                        },
                    )
            else:
                # LLM finished without tool call
                break

        # Fall-closed refusal if no valid draft tool call completed
        return ClinicalNoteDraft(
            workflow_id=verdict.workflow_id,
            draft_id=draft_digest(REFUSAL),
            note=REFUSAL,
            asserted_claims=(),
            excluded_claims=excluded,
            deferred_claims=(),
            citations=(),
            refused=True,
            safety_status=verdict.status,
            can_proceed=verdict.can_proceed,
            requires_review=True,
            evidence_trace_ids=tuple(evidence_trace_ids),
            quarantined_chunks_count=quarantined_chunks_count,
            metadata={"refusal_reason": "No successful drafting tool call completed within iteration limit."},
        )
