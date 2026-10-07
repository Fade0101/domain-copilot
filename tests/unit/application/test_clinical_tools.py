"""Six-tool I/O, fixed permissions, evidence reuse and non-side-effecting drafting."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.application.clinical_tools.contracts import (
    MAX_ARGUMENT_BYTES,
    SearchCorpusInput,
    SearchCorpusOutput,
    ToolName,
    ToolOwner,
)
from app.application.clinical_tools.errors import ToolInputError, ToolOutputError
from app.application.clinical_tools.permissions import TOOL_POLICIES
from app.application.errors import ProviderUnavailableError
from app.application.ports.llm import ToolCall
from app.application.qa.grounding import REFUSAL
from app.application.retrieval.dto import Citation
from app.domain.auth.value_objects import ResourceType, Role, UserId
from app.infrastructure.clinical_tools.contracts import ClinicalToolContracts
from tests.support.clinical_tool_fakes import DOSE_CLAIM, arguments, bind, tool_harness
from tests.support.knowledge_fakes import harness, hit

EXPECTED = {
    "search_corpus": ToolOwner.GUIDELINE_RESEARCHER,
    "retrieve_drug_info": ToolOwner.GUIDELINE_RESEARCHER,
    "check_interactions": ToolOwner.SAFETY_CHECKER,
    "validate_dosage": ToolOwner.SAFETY_CHECKER,
    "draft_clinical_note": ToolOwner.DOCUMENTATION_DRAFTER,
    "finalize_clinical_note": ToolOwner.ORCHESTRATOR,
}


def test_exactly_six_named_tools_with_closed_typed_schemas() -> None:
    assert {name.value for name in ToolName} == set(EXPECTED)
    assert {name.value: policy.owner for name, policy in TOOL_POLICIES.items()} == EXPECTED
    codec = ClinicalToolContracts()
    for name in ToolName:
        definition = codec.definition(name)
        assert definition.name == name.value
        for schema in (definition.input_schema, codec.output_schema(name)):
            assert schema["type"] == "object"
            assert schema["additionalProperties"] is False
            assert schema["properties"]
        assert not {"role", "agent", "approved", "user_id"} & set(
            definition.input_schema["properties"]
        )
    assert not hasattr(codec, "register")
    with pytest.raises(TypeError):
        TOOL_POLICIES[ToolName.SEARCH_CORPUS] = TOOL_POLICIES[ToolName.FINALIZE_CLINICAL_NOTE]  # type: ignore[index]


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("owner", list(ToolOwner))
@pytest.mark.parametrize("name", list(ToolName))
async def test_every_agent_role_tool_combination_is_enforced_at_execution(
    role: Role, owner: ToolOwner, name: ToolName
) -> None:
    setup = tool_harness(role)
    executor = bind(setup.factory, owner, setup.principal, setup.workflow_id)
    advertised = {definition.name for definition in executor.definitions()}
    assert advertised == {key for key, allowed in EXPECTED.items() if allowed == owner}
    result = await executor.execute(
        ToolCall(
            "call-1", name.value, json.dumps(arguments(name, setup.workflow_id, setup.approval_id))
        )
    )
    payload = json.loads(result.output)
    allowed = EXPECTED[name.value] == owner
    assert payload["ok"] is allowed
    if not allowed:
        assert payload["error"]["code"] == "PERMISSION_DENIED"
        assert not setup.knowledge.store.calls
    assert len(setup.finalizer.calls) == int(allowed and name == ToolName.FINALIZE_CLINICAL_NOTE)
    if owner != ToolOwner.ORCHESTRATOR:
        assert executor._finalizer is None
    event = setup.knowledge.audit.entries[-1]
    assert event.actor_id == setup.principal.user_id.value
    assert event.actor_role == role.value
    assert event.outcome == (
        "denied" if not allowed else "completed" if setup.finalizer.calls else "refused"
    )
    details = json.loads(event.detail["telemetry"])
    assert details["agent_scope"] == owner.value
    assert details["latency_ms"] >= 0


@pytest.mark.parametrize("extra", ["approved", "agent", "role", "user_id", "note"])
async def test_claimed_approval_agent_role_or_final_text_is_not_an_argument(extra: str) -> None:
    setup = tool_harness()
    executor = setup.factory.for_orchestrator(setup.principal, setup.workflow_id)
    data = arguments(ToolName.FINALIZE_CLINICAL_NOTE, setup.workflow_id, setup.approval_id)
    data[extra] = True if extra == "approved" else "caller-controlled"
    result = await executor.execute(ToolCall("spoof", "finalize_clinical_note", json.dumps(data)))
    assert json.loads(result.output)["error"]["code"] == "INVALID_ARGUMENTS"
    assert not setup.finalizer.calls
    assert "caller-controlled" not in result.output


@pytest.mark.parametrize("name", ["approve_clinical_note", "write_note", "checkpoint", "unknown"])
async def test_no_seventh_agent_callable_tool(name: str) -> None:
    setup = tool_harness()
    result = await setup.factory.for_orchestrator(setup.principal, setup.workflow_id).execute(
        ToolCall("seventh", name, "{}")
    )
    assert json.loads(result.output)["error"]["code"] == "UNKNOWN_TOOL"
    assert not setup.finalizer.calls
    assert setup.knowledge.audit.entries[-1].action == "clinical_tool.unknown"


@pytest.mark.parametrize(
    ("name", "data"),
    [
        (ToolName.SEARCH_CORPUS, {"query": ""}),
        (ToolName.SEARCH_CORPUS, {"query": True}),
        (ToolName.SEARCH_CORPUS, {"query": "a" * 2001}),
        (ToolName.SEARCH_CORPUS, {"query": "bad\x00query"}),
        (ToolName.RETRIEVE_DRUG_INFO, {"drug": "Synthex", "topic": "invented"}),
        (ToolName.RETRIEVE_DRUG_INFO, {"drug": 123}),
        (ToolName.CHECK_INTERACTIONS, {"drugs": ["Synthex"]}),
        (ToolName.CHECK_INTERACTIONS, {"drugs": ["Synthex", "SYNTHEX"]}),
        (ToolName.CHECK_INTERACTIONS, {"drugs": "Synthex"}),
        (ToolName.VALIDATE_DOSAGE, {"drug": "", "dosage_claim": "5 mg"}),
        (ToolName.VALIDATE_DOSAGE, {"drug": "Synthex", "dosage_claim": False}),
        (ToolName.DRAFT_CLINICAL_NOTE, {"clinical_question": "query", "case_summary": ""}),
        (ToolName.DRAFT_CLINICAL_NOTE, {"clinical_question": "query", "case_summary": "x" * 4001}),
        (
            ToolName.FINALIZE_CLINICAL_NOTE,
            {"workflow_id": "invalid", "approval_id": "invalid", "draft_id": "invalid"},
        ),
    ],
)
def test_invalid_input_schemas(name: ToolName, data: dict[str, Any]) -> None:
    with pytest.raises(ToolInputError):
        ClinicalToolContracts().decode(name, json.dumps(data))


@pytest.mark.parametrize(
    "raw",
    [
        "null",
        "[]",
        "{",
        '{"query":"a","query":"b"}',
        '{"query":NaN}',
        " " * (MAX_ARGUMENT_BYTES + 1),
    ],
    ids=["null", "array", "truncated", "duplicate", "nonfinite", "too_large"],
)
def test_malformed_duplicate_and_unbounded_json_is_rejected(raw: str) -> None:
    with pytest.raises(ToolInputError):
        ClinicalToolContracts().decode(ToolName.SEARCH_CORPUS, raw)


def test_direct_typed_inputs_and_output_serialization_validate_invariants() -> None:
    with pytest.raises(ToolInputError):
        SearchCorpusInput(" ")
    result = SearchCorpusOutput((), True, uuid4())
    codec = ClinicalToolContracts()
    assert codec.encode(ToolName.SEARCH_CORPUS, result)["refused"] is True
    object.__setattr__(result, "refused", False)
    with pytest.raises(ToolOutputError):
        codec.encode(ToolName.SEARCH_CORPUS, result)
    with pytest.raises(ToolOutputError):
        codec.encode(ToolName.DRAFT_CLINICAL_NOTE, result)


@pytest.mark.parametrize("score", [float("nan"), float("inf"), -1.0, 1.1])
def test_invalid_citation_scores_cannot_be_returned(score: float) -> None:
    with pytest.raises(ToolInputError):
        SearchCorpusOutput(
            (Citation(uuid4(), "synthetic.md", None, 1, uuid4(), score, "synthetic evidence"),),
            False,
            uuid4(),
        )


async def test_search_uses_existing_dense_fts_rrf_reranking_and_trace() -> None:
    knowledge = harness(dense=[hit(1)], keyword=[hit(1)])
    setup = tool_harness(knowledge=knowledge)
    result = await setup.factory.for_guideline_researcher(
        setup.principal, setup.workflow_id
    ).execute(ToolCall("read", "search_corpus", '{"query":"clinic hours"}'))
    payload = json.loads(result.output)
    assert payload["result"]["citations"][0]["chunk_id"] == str(UUID(int=1))
    assert knowledge.store.calls == [("dense", 20), ("keyword", 20)]
    event = next(entry for entry in knowledge.audit.entries if entry.action == "retrieval.hybrid")
    assert json.loads(event.detail["telemetry"])["rrf_k"] == 60
    assert not setup.finalizer.calls


@pytest.mark.parametrize(
    "claim",
    [DOSE_CLAIM, DOSE_CLAIM.replace("5 mg", "50 mg"), DOSE_CLAIM.replace("mg", "mcg")],
)
async def test_dosage_verifies_only_the_complete_supported_claim(claim: str) -> None:
    setup = tool_harness(knowledge=harness(dense=[hit(1, DOSE_CLAIM)]))
    result = await setup.factory.for_safety_checker(setup.principal, setup.workflow_id).execute(
        ToolCall(
            "dose", "validate_dosage", json.dumps({"drug": "Synthex-A", "dosage_claim": claim})
        )
    )
    output = json.loads(result.output)["result"]
    assert output["refused"] is (claim != DOSE_CLAIM)
    assert output["status"] == ("SUPPORTED_BY_CORPUS" if claim == DOSE_CLAIM else "NOT_VERIFIED")
    if claim != DOSE_CLAIM:
        assert output["evidence"] == REFUSAL and output["citations"] == []
    assert not setup.finalizer.calls


@pytest.mark.parametrize("scenario", ["empty", "low_score", "conflict", "unspecified", "negated"])
async def test_safety_evidence_refuses_without_inference(scenario: str) -> None:
    hits = [hit(1, DOSE_CLAIM)]
    scores = None
    if scenario == "empty":
        hits = []
    elif scenario == "low_score":
        scores = {UUID(int=1): 0.1}
    elif scenario == "conflict":
        hits.append(hit(2, DOSE_CLAIM.replace("5 mg", "10 mg")))
    elif scenario == "unspecified":
        hits = [hit(1, "Synthex-A dosage is not specified.")]
    elif scenario == "negated":
        hits = [hit(1, "Do not assume that " + DOSE_CLAIM)]
    setup = tool_harness(knowledge=harness(dense=hits, scores=scores))
    result = await setup.factory.for_safety_checker(setup.principal, setup.workflow_id).execute(
        ToolCall(
            "dose", "validate_dosage", json.dumps({"drug": "Synthex-A", "dosage_claim": DOSE_CLAIM})
        )
    )
    output = json.loads(result.output)["result"]
    assert output["refused"] is True and output["evidence"] == REFUSAL


async def test_interaction_absence_is_not_reported_as_a_safe_combination() -> None:
    setup = tool_harness(
        knowledge=harness(dense=[hit(1, "No data are available about interactions.")])
    )
    result = await setup.factory.for_safety_checker(setup.principal, setup.workflow_id).execute(
        ToolCall("interactions", "check_interactions", '{"drugs":["Synthex-A","Synthex-B"]}')
    )
    output = json.loads(result.output)["result"]
    assert output["refused"] is True
    assert output["evidence"] == REFUSAL


async def test_draft_is_deterministic_unverified_review_required_and_never_written() -> None:
    setup = tool_harness(knowledge=harness(dense=[hit(1)]))
    executor = setup.factory.for_documentation_drafter(setup.principal, setup.workflow_id)
    data = arguments(ToolName.DRAFT_CLINICAL_NOTE, setup.workflow_id, setup.approval_id)
    first = json.loads(
        (await executor.execute(ToolCall("draft1", "draft_clinical_note", json.dumps(data)))).output
    )["result"]
    second = json.loads(
        (await executor.execute(ToolCall("draft2", "draft_clinical_note", json.dumps(data)))).output
    )["result"]
    assert first["draft_id"] == second["draft_id"]
    assert first["note"] == second["note"]
    assert first["requires_review"] is True
    assert json.loads(first["note"])["case_context"]["verification"] == "unverified"
    assert first["citations"]
    assert not setup.finalizer.calls
    tool_event = setup.knowledge.audit.entries[-1]
    assert str(data["case_summary"]) not in str(tool_event.detail)
    assert first["note"] not in str(tool_event.detail)


async def test_forged_or_stale_role_is_replaced_before_ownership_check() -> None:
    setup = tool_harness()
    different = uuid4()
    setup.ownership.register(
        ResourceType.RUN,
        str(different),
        UserId(str(uuid4())),
    )
    forged = replace(setup.principal, role=Role.ADMIN)
    result = await setup.factory.for_orchestrator(forged, different).execute(
        ToolCall("forged", "finalize_clinical_note", "{}")
    )
    assert json.loads(result.output)["error"]["code"] == "PERMISSION_DENIED"
    assert setup.knowledge.audit.entries[-1].actor_role == "analyst"
    assert not setup.finalizer.calls


async def test_deleted_actor_and_provider_failure_produce_safe_errors() -> None:
    setup = tool_harness(knowledge=harness(dense=[hit(1)]))
    executor = setup.factory.for_guideline_researcher(setup.principal, setup.workflow_id)
    setup.knowledge.llm.failure = ProviderUnavailableError("PRIVATE_PROVIDER_DETAIL")
    failed = await executor.execute(
        ToolCall("provider", "retrieve_drug_info", '{"drug":"Synthex"}')
    )
    assert json.loads(failed.output)["error"]["code"] == "TOOL_UNAVAILABLE"
    assert "PRIVATE_PROVIDER_DETAIL" not in failed.output
    setup.users.users.clear()
    denied = await executor.execute(ToolCall("deleted", "search_corpus", '{"query":"synthetic"}'))
    assert json.loads(denied.output)["error"]["code"] == "NOT_AUTHENTICATED"
