"""Runtime OpenAPI enhancements for role semantics and SSE's non-JSON wire format."""

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app.presentation.api.schemas.errors import ERROR_RESPONSES
from app.presentation.api.schemas.events import (
    AskTokenData,
    JobProgressData,
    JobStreamCompletedData,
    JobTokenData,
)
from app.presentation.api.schemas.knowledge import AnswerResponse, RefusalResponse

ALL_ROLES = ["analyst", "reviewer", "admin"]


def configure_openapi(app: FastAPI, prefix: str) -> None:
    def build() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title, version=app.version, description=app.description, routes=app.routes
        )
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        for model in (
            AskTokenData,
            JobProgressData,
            JobStreamCompletedData,
            JobTokenData,
            AnswerResponse,
            RefusalResponse,
        ):
            definition = model.model_json_schema(ref_template="#/components/schemas/{model}")
            components.update(definition.pop("$defs", {}))
            components[model.__name__] = definition
        for path, methods in schema["paths"].items():
            for method, operation in methods.items():
                if method not in {"get", "post", "put", "patch", "delete"}:
                    continue
                relative = path.removeprefix(prefix)
                for status in ERROR_RESPONSES:
                    response = operation["responses"].get(str(status))
                    if response is not None:
                        response["content"] = {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                            }
                        }
                        if status == 401:
                            response["headers"] = {
                                "WWW-Authenticate": {
                                    "schema": {"type": "string", "const": "Bearer"}
                                }
                            }
                roles = ALL_ROLES
                ownership = "Authenticated caller; no owner-selected resource."
                if relative.startswith("/jobs"):
                    ownership = "Owner only for analyst/reviewer; admin may access all owned jobs."
                    if method == "post" and (relative == "/jobs" or relative.endswith("/retry")):
                        roles = ["admin"]
                elif relative.startswith("/evaluations"):
                    ownership = "Owner only for analyst/reviewer; admin may access all owned jobs."
                    if method == "post":
                        roles = ["admin"]
                elif relative.startswith("/sessions") or relative == "/ask":
                    ownership = "Session resources are owner-only for every role, including admin."
                elif "/approval" in relative:
                    roles = ["reviewer", "admin"]
                    ownership = "VIEW_ALL_RUNS permits cross-owner clinical review."
                elif relative.startswith("/runs"):
                    ownership = "Owner only for analyst; reviewer/admin may view all runs."
                elif relative.startswith("/traces"):
                    ownership = "Owner only for analyst/reviewer; admin has observability access."
                elif relative.startswith("/documents"):
                    if method == "post":
                        roles = ["admin"]
                    else:
                        ownership = "Owner only; admin may access all ingested documents."
                if operation.get("security"):
                    operation["x-roles"] = roles
                    operation["x-ownership"] = ownership
                    operation["description"] = (
                        operation.get("description", "")
                        + "\n\nRoles: "
                        + ", ".join(roles)
                        + ". "
                        + ownership
                        + " Identity and current role come from the validated JWT and stored user."
                    ).strip()
                if relative.startswith("/jobs/") and relative.endswith(("/events", "/stream")):
                    operation["responses"]["200"] = {
                        "description": "SSE: ordered durable events; id equals sequence_number. "
                        "Heartbeat comments carry no data. A terminal replay drains then closes. "
                        "After headers, storage/auth failures close without a completion event.",
                        "content": {"text/event-stream": {"schema": {"type": "string"}}},
                        "headers": {
                            "Cache-Control": {"schema": {"type": "string"}},
                            "X-Accel-Buffering": {"schema": {"type": "string"}},
                        },
                    }
                    operation["x-sse-events"] = {
                        name: {"$ref": "#/components/schemas/" + model}
                        for name, model in {
                            "job_progress": "JobProgressData",
                            "token": "JobTokenData",
                            "stream_completed": "JobStreamCompletedData",
                        }.items()
                    }
                if relative == "/ask":
                    operation["responses"]["200"]["content"]["text/event-stream"] = {
                        "schema": {"type": "string"},
                    }
                    operation["x-sse-events"] = {
                        name: {"$ref": "#/components/schemas/" + model}
                        for name, model in {
                            "token": "AskTokenData",
                            "stream_completed": "AnswerResponse",
                            "refusal": "RefusalResponse",
                        }.items()
                    }
                if (
                    method == "post"
                    and relative in {"/evaluations", "/jobs", "/sessions"}
                    or (
                        method == "post"
                        and relative.startswith("/jobs/")
                        and relative.endswith("/retry")
                    )
                ):
                    success_status = "201" if relative == "/sessions" else "202"
                    operation["responses"][success_status]["headers"] = {
                        "Location": {
                            "description": "Relative resource/status URL.",
                            "schema": {"type": "string"},
                        }
                    }
        app.openapi_schema = schema
        return schema

    app.openapi = build  # type: ignore[method-assign]  # FastAPI's documented OpenAPI hook.
