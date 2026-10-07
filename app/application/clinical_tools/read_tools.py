"""Five tools with no final-note writer, repository, or persistence capability.

Clinical evidence goes through the real #10 use cases. Drafting is a deterministic
assembly of that evidence and explicitly unverified case context, not an agent,
a new generation policy, or a clinical Safety Checker.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from uuid import UUID

from app.application.auth.context import Principal
from app.application.clinical_tools.contracts import (
    CheckInteractionsInput,
    CheckInteractionsOutput,
    DosageEvidenceStatus,
    DraftClinicalNoteInput,
    DraftClinicalNoteOutput,
    RetrieveDrugInfoInput,
    RetrieveDrugInfoOutput,
    SearchCorpusInput,
    SearchCorpusOutput,
    ValidateDosageInput,
    ValidateDosageOutput,
    draft_digest,
)
from app.application.qa.grounding import REFUSAL
from app.application.qa.use_cases import AskUseCase
from app.application.retrieval.use_cases import HybridRetrievalUseCase


class ReadClinicalTools:
    __slots__ = ("_retrieval", "_ask")

    def __init__(self, retrieval: HybridRetrievalUseCase, ask: AskUseCase) -> None:
        self._retrieval = retrieval
        self._ask = ask

    async def search_corpus(
        self, request: SearchCorpusInput, principal: Principal
    ) -> SearchCorpusOutput:
        result = await self._retrieval.execute(request.query, principal)
        return SearchCorpusOutput(result.citations, not result.selected, UUID(result.trace_id))

    async def retrieve_drug_info(
        self, request: RetrieveDrugInfoInput, principal: Principal
    ) -> RetrieveDrugInfoOutput:
        result = await self._ask.execute(
            f"What {request.topic.value} information is documented for the drug {request.drug}?",
            principal,
        )
        return RetrieveDrugInfoOutput(
            result.answer,
            result.citations,
            result.refused,
            UUID(result.trace_id),
            request.drug,
            request.topic,
        )

    async def check_interactions(
        self, request: CheckInteractionsInput, principal: Principal
    ) -> CheckInteractionsOutput:
        query = f"What interactions are documented for these drugs: {', '.join(request.drugs)}?"
        if request.conditions:
            query += f" Relevant conditions: {', '.join(request.conditions)}."
        result = await self._ask.execute(query, principal)
        # The result is source evidence, never an inferred 'safe combination'.
        return CheckInteractionsOutput(
            result.answer,
            result.citations,
            result.refused,
            UUID(result.trace_id),
            request.drugs,
            request.conditions,
        )

    async def validate_dosage(
        self, request: ValidateDosageInput, principal: Principal
    ) -> ValidateDosageOutput:
        result = await self._ask.execute(
            f"What explicit dosage evidence verifies this complete claim for {request.drug}? "
            + request.dosage_claim,
            principal,
        )
        normalized = " ".join(request.dosage_claim.casefold().split())
        drug = " ".join(request.drug.casefold().split())
        # An exact *whole chunk* match preserves qualifiers/negations and context.
        # This deliberately makes no unit conversions, clinical inference, or
        # substring-based assertion that a different dose is correct.
        supported = (
            not result.refused
            and re.search(r"(?<!\w)" + re.escape(drug) + r"(?!\w)", normalized) is not None
            and any(
                normalized == " ".join(citation.text_snippet.casefold().split())
                for citation in result.citations
            )
        )
        return ValidateDosageOutput(
            result.answer if supported else REFUSAL,
            result.citations if supported else (),
            not supported,
            UUID(result.trace_id),
            request.drug,
            request.dosage_claim,
            (
                DosageEvidenceStatus.SUPPORTED_BY_CORPUS
                if supported
                else DosageEvidenceStatus.NOT_VERIFIED
            ),
        )

    async def draft_clinical_note(
        self, request: DraftClinicalNoteInput, principal: Principal, workflow_id: UUID
    ) -> DraftClinicalNoteOutput:
        result = await self._ask.execute(request.clinical_question, principal)
        note = json.dumps(
            {
                "schema_version": 1,
                "kind": "clinical_note_draft",
                "workflow_id": str(workflow_id),
                "clinical_question": request.clinical_question,
                "case_context": {"text": request.case_summary, "verification": "unverified"},
                "corpus_evidence": result.answer,
                "citations": [asdict(citation) for citation in result.citations],
                "limitations": [
                    "Case context is user supplied and requires clinician verification.",
                    "Corpus quotations are evidence, not patient-specific recommendations.",
                    "No clinical Safety Checker or human review has been performed by this tool.",
                    *([REFUSAL] if result.refused else []),
                ],
                "requires_review": True,
            },
            default=str,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return DraftClinicalNoteOutput(
            workflow_id,
            draft_digest(note),
            note,
            result.citations,
            result.refused,
            UUID(result.trace_id),
        )
