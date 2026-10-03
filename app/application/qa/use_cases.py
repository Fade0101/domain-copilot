"""Grounded ask: retrieve, select evidence via #7, quote sources or refuse."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from time import perf_counter

from app.application.auth.context import Principal
from app.application.errors import KnowledgeUnavailableError, ProviderError
from app.application.ports.llm import CompletionRequest, ILLMProvider, ModelOptions
from app.application.ports.prompts import IPromptProvider
from app.application.ports.system import IIdGenerator
from app.application.qa.grounding import (
    REFUSAL,
    decode_evidence_selection,
    has_direct_conflict,
    has_explicit_clinical_evidence,
)
from app.application.retrieval.dto import Citation, RankedCandidate
from app.application.retrieval.observability import RetrievalObserver
from app.application.retrieval.use_cases import HybridRetrievalUseCase, validate_query


@dataclass(frozen=True, slots=True)
class AskResult:
    answer: str
    citations: tuple[Citation, ...]
    refused: bool
    trace_id: str


class AskUseCase:
    def __init__(
        self,
        retrieval: HybridRetrievalUseCase,
        llm: ILLMProvider,
        prompts: IPromptProvider,
        observer: RetrievalObserver,
        id_generator: IIdGenerator,
        *,
        max_tokens: int = 1024,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._retrieval = retrieval
        self._llm = llm
        self._prompts = prompts
        self._observer = observer
        self._ids = id_generator
        self._max_tokens = max_tokens
        self._timeout = timeout_seconds

    async def execute(self, question: str, principal: Principal) -> AskResult:
        question = validate_query(question)
        trace_id = self._ids.new_id()
        started_at = self._observer.clock.now()
        started = perf_counter()
        telemetry: dict[str, object] = {"refused": True, "cited_chunk_ids": []}
        outcome = "error"
        try:
            evidence = await self._retrieval.execute(question, principal, trace_id=trace_id)
            telemetry.update(
                dense_count=evidence.dense_count,
                keyword_count=evidence.keyword_count,
                fused_count=evidence.fused_count,
                selected_chunk_ids=[str(item.hit.chunk_id) for item in evidence.selected],
            )
            reason = "insufficient_evidence"
            chosen: tuple[RankedCandidate, ...] = ()
            if has_direct_conflict(evidence.selected):
                reason = "conflicting_evidence"
            elif evidence.selected and has_explicit_clinical_evidence(question, evidence.selected):
                prompt = self._prompts.get("grounded_answer", version=2)
                request = CompletionRequest(
                    messages=[
                        {"role": "system", "content": prompt.render()},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "question": question,
                                    "evidence": [
                                        {
                                            "chunk_id": str(item.hit.chunk_id),
                                            "text": item.hit.snippet,
                                        }
                                        for item in evidence.selected
                                    ],
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ],
                    model_options=ModelOptions(
                        temperature=0.0, max_tokens=self._max_tokens, timeout_seconds=self._timeout
                    ),
                )
                phase = perf_counter()
                async with asyncio.timeout(self._timeout):
                    response = await self._llm.complete(request)
                telemetry["generation_latency_ms"] = (perf_counter() - phase) * 1000
                telemetry["prompt_id"] = prompt.id
                telemetry["prompt_version"] = prompt.version
                telemetry["usage"] = response.usage or {}
                chosen = (
                    decode_evidence_selection(response.content, evidence.selected)
                    if not response.tool_calls
                    else ()
                )
                if chosen and not has_explicit_clinical_evidence(question, chosen):
                    chosen = ()
                reason = "model_refusal_or_invalid_grounding"
            if not chosen:
                telemetry["refusal_reason"] = reason
                outcome = "refused"
                return AskResult(REFUSAL, (), True, trace_id)
            citations = tuple(Citation.from_candidate(candidate) for candidate in chosen)
            # Clinical prose is copied from complete indexed chunks, not trusted
            # from the completion. [n] resolves to citations[n - 1].
            answer = "\n\n".join(
                f"[{index}] {citation.text_snippet}" for index, citation in enumerate(citations, 1)
            )
            telemetry["refused"] = False
            telemetry["cited_chunk_ids"] = [str(citation.chunk_id) for citation in citations]
            outcome = "completed"
            return AskResult(answer, citations, False, trace_id)
        except (ProviderError, TimeoutError) as exc:
            raise KnowledgeUnavailableError("Grounded answer generation is unavailable") from exc
        finally:
            telemetry["latency_ms"] = (perf_counter() - started) * 1000
            await self._observer.record(
                action="qa.ask",
                principal=principal,
                trace_id=trace_id,
                query=question,
                started_at=started_at,
                telemetry=telemetry,
                outcome=outcome,
            )
