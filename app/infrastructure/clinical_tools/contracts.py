"""Pydantic is confined to this adapter; the six contracts remain plain dataclasses."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from types import MappingProxyType
from typing import Any, cast
from uuid import UUID

from pydantic import ConfigDict, TypeAdapter, ValidationError
from pydantic.dataclasses import dataclass as validated_dataclass

from app.application.clinical_tools.contracts import (
    MAX_ARGUMENT_BYTES,
    CheckInteractionsInput,
    CheckInteractionsOutput,
    DraftClinicalNoteInput,
    DraftClinicalNoteOutput,
    FinalizeClinicalNoteInput,
    FinalizeClinicalNoteOutput,
    RetrieveDrugInfoInput,
    RetrieveDrugInfoOutput,
    SearchCorpusInput,
    SearchCorpusOutput,
    ToolInput,
    ToolName,
    ToolOutput,
    ValidateDosageInput,
    ValidateDosageOutput,
)
from app.application.clinical_tools.errors import ToolInputError, ToolOutputError
from app.application.ports.clinical_tools import IClinicalToolContracts
from app.application.ports.llm import ToolDefinition

# A closed catalog, not a registration mechanism. Neither configuration nor a
# model completion can add tools or replace an implementation.
_CONTRACTS = MappingProxyType(
    {
        ToolName.SEARCH_CORPUS: (
            SearchCorpusInput,
            SearchCorpusOutput,
            "Read-only hybrid corpus search with indexed citations.",
        ),
        ToolName.RETRIEVE_DRUG_INFO: (
            RetrieveDrugInfoInput,
            RetrieveDrugInfoOutput,
            "Read-only, grounded drug information; refuse without explicit corpus evidence.",
        ),
        ToolName.CHECK_INTERACTIONS: (
            CheckInteractionsInput,
            CheckInteractionsOutput,
            "Read-only interaction evidence. Missing evidence never means no interaction.",
        ),
        ToolName.VALIDATE_DOSAGE: (
            ValidateDosageInput,
            ValidateDosageOutput,
            "Verify a complete dosage claim against verbatim corpus evidence; never infer doses.",
        ),
        ToolName.DRAFT_CLINICAL_NOTE: (
            DraftClinicalNoteInput,
            DraftClinicalNoteOutput,
            "Return a structured, cited draft requiring review. Does not persist a note.",
        ),
        ToolName.FINALIZE_CLINICAL_NOTE: (
            FinalizeClinicalNoteInput,
            FinalizeClinicalNoteOutput,
            "Persist only the reviewed text of the matching authoritative APPROVED record.",
        ),
    }
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ToolInputError("Duplicate argument field")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ToolInputError("Non-finite JSON values are forbidden")


def _json_default(value: object) -> str:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("Unsupported tool result value")


class ClinicalToolContracts(IClinicalToolContracts):
    def __init__(self) -> None:
        config = ConfigDict(extra="forbid", strict=True, revalidate_instances="always")
        self._inputs: dict[ToolName, TypeAdapter[Any]] = {}
        self._outputs: dict[ToolName, TypeAdapter[Any]] = {}
        for name, (input_type, output_type, _) in _CONTRACTS.items():
            self._inputs[name] = TypeAdapter(
                validated_dataclass(input_type, config=config, frozen=True)
            )
            self._outputs[name] = TypeAdapter(
                validated_dataclass(output_type, config=config, frozen=True)
            )

    def definition(self, name: ToolName) -> ToolDefinition:
        return ToolDefinition(
            name=name.value,
            description=_CONTRACTS[name][2],
            input_schema=deepcopy(self._inputs[name].json_schema()),
        )

    def output_schema(self, name: ToolName) -> dict[str, Any]:
        return deepcopy(self._outputs[name].json_schema())

    def decode(self, name: ToolName, arguments: str) -> ToolInput:
        try:
            if (
                not isinstance(arguments, str)
                or len(arguments.encode("utf-8")) > MAX_ARGUMENT_BYTES
            ):
                raise ToolInputError("Arguments exceed the supported size")
            payload = json.loads(
                arguments, object_pairs_hook=_unique_object, parse_constant=_reject_constant
            )
            if not isinstance(payload, dict):
                raise ToolInputError("Arguments must be an object")
            # JSON mode permits UUID strings/arrays but strict=True still rejects
            # coerced scalars, booleans as numbers, and unexpected role/agent flags.
            return cast(ToolInput, self._inputs[name].validate_json(arguments))
        except (ValidationError, ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise ToolInputError("Invalid clinical tool arguments") from exc

    def encode(self, name: ToolName, result: ToolOutput) -> dict[str, Any]:
        try:
            if not isinstance(result, _CONTRACTS[name][1]):
                raise ToolOutputError("Wrong output type for clinical tool")
            encoded = json.dumps(asdict(result), default=_json_default, allow_nan=False)
            validated = self._outputs[name].validate_json(encoded)
            return cast(dict[str, Any], self._outputs[name].dump_python(validated, mode="json"))
        except (
            ValidationError,
            ValueError,
            TypeError,
            UnicodeError,
            RecursionError,
            ToolInputError,
        ) as exc:
            raise ToolOutputError("Invalid clinical tool result") from exc
