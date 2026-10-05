"""Guideline Researcher Agent (AGT-01).

Researches clinical guidelines and drug information using exactly two server-bound
tools (search_corpus and retrieve_drug_info), enforcing tool-derived citation
provenance, deterministic sufficiency rules, end-to-end monotonic timeouts, and
Ticket #13 prompt-injection containment.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any
from uuid import UUID

from app.application.agents.contracts import CaseSummary, ResearchFindings, TerminationReason
from app.application.clinical_tools.contracts import ToolName
from app.application.clinical_tools.execution import ClinicalToolExecutor
from app.application.ports.audit import AuditEntry
from app.application.ports.llm import (
    CompletionRequest,
    CompletionResponse,
    ILLMProvider,
    ModelOptions,
)
from app.application.ports.prompts import IPromptProvider
from app.application.qa.evidence_boundary import instruction_signals
from app.application.qa.grounding import (
    _CONTRA_QUERY,
    _CONTRA_STATEMENT,
    _DOSE_CONTEXT,
    _DOSE_QUERY,
    _DOSE_VALUE,
    _INTERACTION_QUERY,
    _INTERACTION_STATEMENT,
    _UNSPECIFIED,
    REFUSAL,
)
from app.application.retrieval.dto import Citation
from app.application.retrieval.observability import RetrievalObserver

ALLOWED_TOOLS = frozenset({ToolName.SEARCH_CORPUS.value, ToolName.RETRIEVE_DRUG_INFO.value})
DEFAULT_MAX_ITERATIONS = 4
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_PROMPT_VERSION = 1


def _check_clinical_relevance(question: str, citations: tuple[Citation, ...]) -> bool:
    """Deterministic check requiring explicit statements for high-risk categories."""
    if not citations:
        return False
    sentences = [
        sentence
        for citation in citations
        for sentence in re.split(r"(?<=[.!?])\s+|\n", citation.text_snippet)
        if not _UNSPECIFIED.search(sentence)
    ]
    if _DOSE_QUERY.search(question) and not any(
        _DOSE_VALUE.search(sentence) and _DOSE_CONTEXT.search(sentence) for sentence in sentences
    ):
        return False
    if _CONTRA_QUERY.search(question) and not any(_CONTRA_STATEMENT.search(s) for s in sentences):
        return False
    if _INTERACTION_QUERY.search(question) and not any(
        _INTERACTION_STATEMENT.search(s) for s in sentences
    ):
        return False
    # Check general term overlap (at least one non-stopword from question in evidence)
    words = [
        w.casefold()
        for w in re.findall(r"\b[A-Za-z0-9-]{3,}\b", question)
        if w.casefold()
        not in {
            "what",
            "which",
            "when",
            "where",
            "does",
            "have",
            "been",
            "with",
            "from",
            "that",
            "this",
            "these",
            "those",
            "about",
            "guide",
            "guideline",
            "clinical",
            "corpus",
            "patient",
            "case",
        }
    ]
    if words:
        all_snippets = " ".join(c.text_snippet.casefold() for c in citations)
        if not any(word in all_snippets for word in words):
            return False
    return True


class _VerifiedEvidenceRegistry:
    """Internal registry storing verified, non-quarantined citations from tool results.

    The LLM is NEVER trusted as a source of citation identity. Only citations
    returned by real tool executions and passing instruction-signal checks are
    eligible to enter ResearchFindings.citations.
    """

    def __init__(self) -> None:
        self._by_chunk_id: dict[UUID, Citation] = {}
        self._ordered: list[Citation] = []

    def register(self, citation: Citation) -> None:
        if citation.chunk_id not in self._by_chunk_id:
            self._by_chunk_id[citation.chunk_id] = citation
            self._ordered.append(citation)

    def contains(self, chunk_id: UUID) -> bool:
        return chunk_id in self._by_chunk_id

    def get(self, chunk_id: UUID) -> Citation | None:
        return self._by_chunk_id.get(chunk_id)

    def all_citations(self) -> tuple[Citation, ...]:
        return tuple(self._ordered)

    def __len__(self) -> int:
        return len(self._ordered)


class GuidelineResearcherAgent:
    """Guideline Researcher Agent (AGT-01).

    Researches clinical guidelines using only search_corpus and retrieve_drug_info.
    """

    def __init__(
        self,
        llm: ILLMProvider,
        tools: ClinicalToolExecutor,
        prompts: IPromptProvider,
        observer: RetrievalObserver | None = None,
        *,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        prompt_version: int = DEFAULT_PROMPT_VERSION,
    ) -> None:
        self._llm = llm
        self._tools = tools
        self._prompts = prompts
        self._observer = observer
        self._max_iterations = max(1, max_iterations)
        self._timeout_seconds = max(0.1, timeout_seconds)
        self._prompt_version = prompt_version

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Expose the exact names of tools available to this agent."""
        return tuple(sorted(d.name for d in self._tools.definitions()))

    @property
    def tools(self) -> ClinicalToolExecutor:
        return self._tools

    async def execute(self, summary: CaseSummary) -> ResearchFindings:
        """Execute guideline research for the given case summary."""
        deadline = time.monotonic() + self._timeout_seconds
        registry = _VerifiedEvidenceRegistry()
        evidence_trace_ids: list[UUID] = []
        quarantined_chunks_count = 0
        quarantined_chunk_ids: list[str] = []
        rejected_tool_calls = 0
        tool_call_count = 0
        iterations = 0
        tool_error_occurred = False

        prompt = self._prompts.get("guideline_researcher", version=self._prompt_version)
        rendered_prompt = prompt.render()

        user_content = json.dumps(
            {
                "case_summary": summary.case_summary,
                "clinical_question": summary.clinical_question,
                "patient_context": summary.patient_context,
            },
            ensure_ascii=False,
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": rendered_prompt},
            {"role": "user", "content": user_content},
        ]

        tool_definitions = self._tools.definitions()

        findings_text = ""
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
                # LLM requested tool executions
                for call in response.tool_calls:
                    tool_call_count += 1
                    # 1. Defense-in-depth tool-call validation
                    if call.name not in ALLOWED_TOOLS:
                        rejected_tool_calls += 1
                        error_payload = {
                            "ok": False,
                            "tool": call.name,
                            "error": {
                                "code": "PERMISSION_DENIED",
                                "message": (
                                    f"Tool '{call.name}' is outside Guideline Researcher scope. "
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

                    # 2. Dispatch to server-scoped executor
                    tool_result = await self._tools.execute(call)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": tool_result.output,
                        }
                    )

                    # 3. Process tool result & evidence in strict order
                    try:
                        payload = json.loads(tool_result.output)
                    except (json.JSONDecodeError, TypeError):
                        tool_error_occurred = True
                        continue

                    if not payload.get("ok"):
                        # Tool execution error
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

                        # 4. Check instruction signals & quarantine hostile evidence
                        signals = instruction_signals(citation.text_snippet)
                        if signals:
                            quarantined_chunks_count += 1
                            quarantined_chunk_ids.append(str(citation.chunk_id))
                            # NEVER enters _VerifiedEvidenceRegistry!
                        else:
                            # 5. Register only non-quarantined citations
                            registry.register(citation)

                if termination_reason == TerminationReason.TIMEOUT:
                    break
            else:
                # LLM finished researching and produced content synthesis
                findings_text = response.content or ""
                break

        # Apply deterministic sufficiency rule
        verified_citations = registry.all_citations()
        final_citations: tuple[Citation, ...] = ()

        if time.monotonic() >= deadline or termination_reason == TerminationReason.TIMEOUT:
            termination_reason = TerminationReason.TIMEOUT
            refused = True
            findings = findings_text if findings_text else REFUSAL
        elif tool_error_occurred and not verified_citations:
            termination_reason = TerminationReason.TOOL_ERROR
            refused = True
            findings = REFUSAL
            final_citations = ()
        elif len(verified_citations) == 0:
            termination_reason = TerminationReason.EMPTY_EVIDENCE
            refused = True
            findings = REFUSAL
            final_citations = ()
        else:
            # Verified citations exist: check relevance and grounding
            is_relevant = _check_clinical_relevance(summary.clinical_question, verified_citations)
            if not is_relevant:
                termination_reason = TerminationReason.LOW_EVIDENCE
                refused = True
                findings = REFUSAL
                final_citations = verified_citations
            else:
                # Sufficient grounded evidence
                termination_reason = TerminationReason.SUFFICIENT_EVIDENCE
                refused = False
                findings = (
                    findings_text
                    if findings_text and findings_text.strip() != REFUSAL
                    else " ".join(
                        f"[{i + 1}] {c.text_snippet}" for i, c in enumerate(verified_citations)
                    )
                )
                final_citations = verified_citations

        telemetry: dict[str, object] = {
            "agent": "guideline_researcher",
            "prompt_version": self._prompt_version,
            "iterations": iterations,
            "tool_call_count": tool_call_count,
            "rejected_tool_calls": rejected_tool_calls,
            "verified_citations_count": len(verified_citations),
            "quarantined_chunks_count": quarantined_chunks_count,
            "quarantined_chunk_ids": quarantined_chunk_ids,
            "termination_reason": termination_reason.value,
        }

        if self._observer:
            await self._observer.sink.record(
                AuditEntry(
                    actor_id="system",
                    actor_role="agent",
                    action="guideline_researcher.execute",
                    outcome="refused" if refused else "completed",
                    occurred_at=self._observer.clock.now(),
                    resource_type="workflow",
                    resource_id=str(summary.workflow_id),
                    correlation_id=str(summary.workflow_id),
                    detail={
                        "query": summary.clinical_question,
                        "telemetry": json.dumps(telemetry, allow_nan=False),
                    },
                )
            )

        return ResearchFindings(
            workflow_id=summary.workflow_id,
            findings=findings,
            citations=final_citations,
            refused=refused,
            termination_reason=termination_reason,
            iterations=iterations,
            prompt_version=self._prompt_version,
            evidence_trace_ids=tuple(evidence_trace_ids),
            quarantined_chunks_count=quarantined_chunks_count,
            metadata=telemetry,
        )
