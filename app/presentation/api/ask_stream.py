"""SSE serialization of already-grounded answers; never raw provider output."""

import json
import re
from collections.abc import AsyncIterator

from app.presentation.api.schemas.knowledge import AnswerResponse, RefusalResponse


async def stream_answer(result: AnswerResponse | RefusalResponse) -> AsyncIterator[str]:
    if result.refused:
        yield f"event: refusal\ndata: {result.model_dump_json()}\n\n"
        return
    for part in re.findall(r"\S+\s*|\s+", result.answer):
        data = json.dumps({"delta": part, "trace_id": str(result.trace_id)}, ensure_ascii=False)
        yield f"event: token\ndata: {data}\n\n"
    yield f"event: stream_completed\ndata: {result.model_dump_json()}\n\n"
