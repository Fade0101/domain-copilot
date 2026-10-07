"""Safety Checker Agent (AGT-02).

Validates drug interactions and dosage safety using only check_interactions
and validate_dosage under an uncompromising unknown-stays-unknown policy.
Pure standard-library application logic; clean architecture boundary.
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import UUID

from app.application.agents.contracts import (
    ResearchFindings,
    SafetyClaimCheck,
    SafetyClaimStatus,
    SafetyClaimType,
    SafetyFlag,
    SafetySeverity,
    SafetyStatus,
    SafetyVerdict,
    TerminationReason,
)
from app.application.agents.evidence import VerifiedEvidenceRegistry
from app.application.clinical_tools.contracts import (
    DosageEvidenceStatus,
    ToolName,
)
from app.application.clinical_tools.execution import ClinicalToolExecutor
from app.application.observability.context import current_trace, get_current_correlation_id
from app.application.observability.recording import observe
from app.application.ports.audit import AuditEntry
from app.application.ports.llm import (
    CompletionRequest,
    CompletionResponse,
    ILLMProvider,
    ModelOptions,
)
from app.application.ports.prompts import IPromptProvider
from app.application.qa.evidence_boundary import instruction_signals
from app.application.qa.grounding import REFUSAL
from app.application.retrieval.dto import Citation
from app.application.retrieval.observability import RetrievalObserver

ALLOWED_TOOLS = frozenset({ToolName.CHECK_INTERACTIONS.value, ToolName.VALIDATE_DOSAGE.value})
DEFAULT_MAX_ITERATIONS = 4
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_PROMPT_VERSION = 1


class SafetyCheckerAgent:
    """Safety Checker Agent (AGT-02).

    Evaluates ResearchFindings for drug interactions and dosage safety using
    strictly scoped clinical tools. Missing evidence remains unknown/unsupported;
    never guesses dosages or absence of interactions.
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

    async def execute(self, findings: ResearchFindings) -> SafetyVerdict:
        async with observe(self._observer, "agent.safety_checker", "agent") as data:
            result = await self._execute(findings)
            data.update(
                outcome="refused" if result.refused else "completed",
                iterations=result.iterations,
                termination_reason=result.termination_reason.value,
            )
            return result

    async def _execute(self, findings: ResearchFindings) -> SafetyVerdict:
        """Execute safety evaluation on the provided research findings."""
        # 1. Fail-closed check on input findings
        if findings.refused or not findings.findings or findings.findings.strip() == REFUSAL:
            return SafetyVerdict(
                workflow_id=findings.workflow_id,
                status=SafetyStatus.UNSUPPORTED,
                can_proceed=False,
                checked_claims=(),
                flags=(),
                reasons=("Input research findings were unverified or refused.",),
                citations=(),
                refused=True,
                termination_reason=findings.termination_reason,
                iterations=0,
                prompt_version=self._prompt_version,
                evidence_trace_ids=(),
                quarantined_chunks_count=findings.quarantined_chunks_count,
                metadata={"reason": "refused_input_findings"},
            )

        deadline = time.monotonic() + self._timeout_seconds
        registry = VerifiedEvidenceRegistry()
        evidence_trace_ids: list[UUID] = []
        quarantined_chunks_count = findings.quarantined_chunks_count
        if instruction_signals(findings.findings):
            quarantined_chunks_count += 1
        quarantined_chunk_ids: list[str] = []
        rejected_tool_calls = 0
        tool_call_count = 0
        iterations = 0
        tool_error_occurred = False

        checked_claims: list[SafetyClaimCheck] = []
        flags: list[SafetyFlag] = []
        reasons: list[str] = []

        prompt = self._prompts.get("safety_checker", version=self._prompt_version)
        rendered_prompt = prompt.render()

        user_content = json.dumps(
            {
                "workflow_id": str(findings.workflow_id),
                "findings": findings.findings,
            },
            ensure_ascii=False,
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": rendered_prompt},
            {"role": "user", "content": user_content},
        ]

        tool_definitions = self._tools.definitions()
        termination_reason = TerminationReason.MAX_ITERATIONS

        while iterations < self._max_iterations:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                termination_reason = TerminationReason.TIMEOUT
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
            except TimeoutError:
                termination_reason = TerminationReason.TIMEOUT
                break
            except Exception:
                termination_reason = TerminationReason.TOOL_ERROR
                tool_error_occurred = True
                break

            if time.monotonic() >= deadline:
                termination_reason = TerminationReason.TIMEOUT
                break

            if response.tool_calls:
                for call in response.tool_calls:
                    tool_call_count += 1
                    # 2. Defense-in-depth tool-call validation
                    if call.name not in ALLOWED_TOOLS:
                        rejected_tool_calls += 1
                        error_payload = {
                            "ok": False,
                            "tool": call.name,
                            "error": {
                                "code": "PERMISSION_DENIED",
                                "message": (
                                    f"Tool '{call.name}' is outside Safety Checker scope. "
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

                    # Check remaining time before executing tool
                    now = time.monotonic()
                    if deadline - now <= 0:
                        termination_reason = TerminationReason.TIMEOUT
                        break

                    # 3. Dispatch to server-scoped executor
                    tool_result = await self._tools.execute(call)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": tool_result.output,
                        }
                    )

                    # 4. Process tool result & evidence in strict order
                    try:
                        payload = json.loads(tool_result.output)
                    except (json.JSONDecodeError, TypeError):
                        tool_error_occurred = True
                        continue

                    if not payload.get("ok"):
                        tool_error_occurred = True
                        continue

                    inner = payload.get("result", {})
                    trace_str = payload.get("trace_id")
                    if trace_str:
                        try:
                            evidence_trace_ids.append(UUID(str(trace_str)))
                        except ValueError:
                            pass

                    # Extract genuine citations
                    raw_citations = inner.get("citations", [])
                    clean_call_citations: list[Citation] = []
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
                            quarantined_chunk_ids.append(str(citation.chunk_id))
                            # NEVER enters registry or claim citations
                        else:
                            registry.register(citation)
                            clean_call_citations.append(citation)

                    tool_clean_citations = tuple(clean_call_citations)

                    # Process specific safety tool claims
                    if call.name == ToolName.VALIDATE_DOSAGE.value:
                        drug = str(inner.get("drug", "unknown"))
                        dosage_claim = str(inner.get("dosage_claim", ""))
                        dosage_status = inner.get("status")

                        if dosage_status == DosageEvidenceStatus.SUPPORTED_BY_CORPUS.value:
                            checked_claims.append(
                                SafetyClaimCheck(
                                    claim_type=SafetyClaimType.DOSAGE,
                                    target=drug,
                                    status=SafetyClaimStatus.VERIFIED_SAFE,
                                    detail=f"Dosage verified by corpus: {dosage_claim}",
                                    citations=tool_clean_citations,
                                )
                            )
                        else:
                            # NOT_VERIFIED must always be UNSUPPORTED (never FLAGGED)
                            checked_claims.append(
                                SafetyClaimCheck(
                                    claim_type=SafetyClaimType.DOSAGE,
                                    target=drug,
                                    status=SafetyClaimStatus.UNSUPPORTED,
                                    detail=f"Dosage not verified in corpus: {dosage_claim}",
                                    citations=tool_clean_citations,
                                )
                            )
                            reasons.append(
                                f"Dosage for {drug} ({dosage_claim}) was not verified in "
                                "the clinical corpus."
                            )

                    elif call.name == ToolName.CHECK_INTERACTIONS.value:
                        drugs = tuple(inner.get("drugs", ()))
                        target_str = ", ".join(drugs) if drugs else "unknown"
                        tool_refused = bool(inner.get("refused", True))
                        evidence_text = str(inner.get("evidence", ""))

                        if not tool_refused and evidence_text and evidence_text != REFUSAL:
                            # Documented interaction returned
                            flag_item = SafetyFlag(
                                claim=f"Drug interaction: {target_str}",
                                severity=SafetySeverity.CRITICAL,
                                reason=evidence_text,
                                citations=tool_clean_citations,
                                evidence_snippet=(
                                    tool_clean_citations[0].text_snippet
                                    if tool_clean_citations
                                    else None
                                ),
                            )
                            flags.append(flag_item)
                            checked_claims.append(
                                SafetyClaimCheck(
                                    claim_type=SafetyClaimType.INTERACTION,
                                    target=target_str,
                                    status=SafetyClaimStatus.FLAGGED,
                                    detail=evidence_text,
                                    citations=tool_clean_citations,
                                )
                            )
                            reasons.append(
                                f"Documented drug interaction detected for {target_str}."
                            )
                        else:
                            # Refused / no documented interaction in corpus -> UNSUPPORTED
                            checked_claims.append(
                                SafetyClaimCheck(
                                    claim_type=SafetyClaimType.INTERACTION,
                                    target=target_str,
                                    status=SafetyClaimStatus.UNSUPPORTED,
                                    detail=(
                                        "NO_DOCUMENTED_INTERACTION; "
                                        "corpus contains no explicit interaction evidence"
                                    ),
                                    citations=tool_clean_citations,
                                )
                            )
                            reasons.append(
                                f"No interaction evidence documented in corpus for {target_str}; "
                                "interaction safety remains unknown."
                            )

                if termination_reason == TerminationReason.TIMEOUT:
                    break
            else:
                # LLM finished without further tool calls
                break

        # Authoritative deterministic decision evaluation
        all_verified_citations = registry.all_citations()

        if time.monotonic() >= deadline or termination_reason == TerminationReason.TIMEOUT:
            status = SafetyStatus.TIMEOUT
            termination_reason = TerminationReason.TIMEOUT
            reasons.append("Safety evaluation timed out.")
        elif tool_error_occurred and not checked_claims:
            status = SafetyStatus.TOOL_ERROR
            termination_reason = TerminationReason.TOOL_ERROR
            reasons.append("Tool execution failure occurred during safety check.")
        elif any(c.status == SafetyClaimStatus.FLAGGED for c in checked_claims) or len(flags) > 0:
            status = SafetyStatus.FLAGGED
            termination_reason = TerminationReason.SUFFICIENT_EVIDENCE
        elif not checked_claims:
            status = SafetyStatus.UNSUPPORTED
            termination_reason = TerminationReason.EMPTY_EVIDENCE
            reasons.append("No clinical safety claims were evaluated.")
        elif any(c.status == SafetyClaimStatus.UNSUPPORTED for c in checked_claims):
            # Unknown-stays-unknown: any unverified claim (dosage or interaction) forces UNSUPPORTED
            status = SafetyStatus.UNSUPPORTED
            termination_reason = TerminationReason.LOW_EVIDENCE
        elif (
            all(c.status == SafetyClaimStatus.VERIFIED_SAFE for c in checked_claims)
            and len(checked_claims) > 0
        ):
            status = SafetyStatus.SAFE
            termination_reason = TerminationReason.SUFFICIENT_EVIDENCE
            reasons.append("All dosage and safety claims explicitly verified safe.")
        else:
            status = SafetyStatus.UNSUPPORTED
            termination_reason = TerminationReason.LOW_EVIDENCE

        can_proceed = status == SafetyStatus.SAFE
        refused = status != SafetyStatus.SAFE

        telemetry: dict[str, object] = {
            "agent": "safety_checker",
            "prompt_version": self._prompt_version,
            "iterations": iterations,
            "tool_call_count": tool_call_count,
            "rejected_tool_calls": rejected_tool_calls,
            "checked_claims_count": len(checked_claims),
            "flags_count": len(flags),
            "verified_citations_count": len(all_verified_citations),
            "quarantined_chunks_count": quarantined_chunks_count,
            "quarantined_chunk_ids": quarantined_chunk_ids,
            "termination_reason": termination_reason.value,
            "status": status.value,
        }

        if self._observer:
            await self._observer.sink.record(
                AuditEntry(
                    actor_id=str(context.user_id) if (context := current_trace()) else "system",
                    actor_role="agent",
                    action="safety_checker.execute",
                    outcome="safe" if can_proceed else "refused",
                    occurred_at=self._observer.clock.now(),
                    resource_type="workflow",
                    resource_id=str(findings.workflow_id),
                    correlation_id=get_current_correlation_id() or str(findings.workflow_id),
                    detail={
                        "status": status.value,
                        "flags_count": str(len(flags)),
                        "telemetry": json.dumps(telemetry, allow_nan=False),
                    },
                )
            )

        return SafetyVerdict(
            workflow_id=findings.workflow_id,
            status=status,
            can_proceed=can_proceed,
            checked_claims=tuple(checked_claims),
            flags=tuple(flags),
            reasons=tuple(reasons),
            citations=all_verified_citations,
            refused=refused,
            termination_reason=termination_reason,
            iterations=iterations,
            prompt_version=self._prompt_version,
            evidence_trace_ids=tuple(evidence_trace_ids),
            quarantined_chunks_count=quarantined_chunks_count,
            metadata=telemetry,
        )
