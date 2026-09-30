import json
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from groq import APIConnectionError, APIStatusError, RateLimitError

from app.application.errors import (
    ContextWindowExceededError,
    ProviderAuthenticationError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from app.application.ports.llm import (
    CompletionRequest,
    CompletionResponse,
    ILLMProvider,
    ModelOptions,
    StreamChunk,
    ToolCall,
    ToolDefinition,
)
from app.infrastructure.llm.fallback import FallbackLLMProvider
from app.infrastructure.llm.groq_adapter import GroqAdapter
from app.infrastructure.llm.ollama_adapter import OllamaAdapter


class FakeILLMProvider:
    def __init__(self, should_fail_with: Exception | None = None):
        self.should_fail_with = should_fail_with
        self.last_request: CompletionRequest | None = None

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.last_request = request
        if self.should_fail_with:
            raise self.should_fail_with
        return CompletionResponse(content="Success")

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        self.last_request = request
        if self.should_fail_with:
            raise self.should_fail_with
        yield StreamChunk(delta="Success")


def test_adapters_implement_illmprovider_protocol():
    groq = GroqAdapter(api_key="fake", client=MagicMock())
    ollama = OllamaAdapter(base_url="http://localhost:11434", client=MagicMock())
    fallback = FallbackLLMProvider(primary=groq, secondary=ollama)

    assert isinstance(groq, ILLMProvider)
    assert isinstance(ollama, ILLMProvider)
    assert isinstance(fallback, ILLMProvider)


# --- FallbackLLMProvider tests ---


@pytest.mark.asyncio
async def test_fallback_transient_error():
    """Test fallback logic passes exact request to secondary on transient error."""
    primary = FakeILLMProvider(should_fail_with=ProviderRateLimitError("Rate limited"))
    secondary = FakeILLMProvider()

    fallback = FallbackLLMProvider(primary=primary, secondary=secondary)

    req = CompletionRequest(
        messages=[{"role": "user", "content": "Hello"}],
        model_options=ModelOptions(temperature=0.7),
    )

    res = await fallback.complete(req)
    assert res.content == "Success"
    assert secondary.last_request is req


@pytest.mark.asyncio
async def test_fallback_stream_transient_error():
    """Test streaming fallback logic passes exact request to secondary on transient error."""
    primary = FakeILLMProvider(should_fail_with=ProviderRateLimitError("Rate limited"))
    secondary = FakeILLMProvider()

    fallback = FallbackLLMProvider(primary=primary, secondary=secondary)

    req = CompletionRequest(
        messages=[{"role": "user", "content": "Hello stream"}],
    )

    chunks = []
    async for chunk in fallback.stream(req):
        chunks.append(chunk)

    assert len(chunks) == 1
    assert chunks[0].delta == "Success"
    assert secondary.last_request is req


@pytest.mark.asyncio
async def test_fallback_no_fallback_on_non_transient():
    """Test fallback logic fails fast on non-transient error."""

    class AuthError(Exception):
        pass

    primary = FakeILLMProvider(should_fail_with=AuthError("Bad Auth"))
    secondary = FakeILLMProvider()

    fallback = FallbackLLMProvider(primary=primary, secondary=secondary)

    req = CompletionRequest(messages=[{"role": "user", "content": "Hello"}])

    with pytest.raises(AuthError):
        await fallback.complete(req)

    assert secondary.last_request is None


# --- GroqAdapter tests ---


@pytest.mark.asyncio
async def test_groq_adapter_complete_success():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_choice = MagicMock()
    mock_choice.message.content = "Groq response"

    mock_tc = MagicMock()
    mock_tc.id = "tc_1"
    mock_tc.function.name = "get_weather"
    mock_tc.function.arguments = '{"location": "Boston"}'
    mock_choice.message.tool_calls = [mock_tc]

    mock_response.choices = [mock_choice]
    mock_response.usage.prompt_tokens = 10
    mock_response.usage.completion_tokens = 5
    mock_response.usage.total_tokens = 15

    mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

    adapter = GroqAdapter(api_key="test-key", default_model="llama-3.3-70b", client=mock_client)

    tool = ToolDefinition(
        name="get_weather",
        description="Get weather",
        input_schema={"type": "object"},
    )
    req = CompletionRequest(
        messages=[{"role": "user", "content": "Weather in Boston?"}],
        model_options=ModelOptions(temperature=0.5, max_tokens=100, timeout_seconds=10.0),
        tools=[tool],
    )

    resp = await adapter.complete(req)

    assert resp.content == "Groq response"
    assert resp.tool_calls == [
        ToolCall(id="tc_1", name="get_weather", arguments='{"location": "Boston"}')
    ]
    assert resp.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    mock_client.chat.completions.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_groq_adapter_stream_success():
    mock_client = MagicMock()

    chunk1 = MagicMock()
    chunk1.choices = [MagicMock()]
    chunk1.choices[0].delta.content = "Hello"
    chunk1.choices[0].delta.tool_calls = None
    chunk1.choices[0].finish_reason = None
    chunk1.x_groq = None

    chunk2 = MagicMock()
    chunk2.choices = [MagicMock()]
    chunk2.choices[0].delta.content = " world"
    chunk2.choices[0].delta.tool_calls = None
    chunk2.choices[0].finish_reason = "stop"
    chunk2.x_groq = {"usage": {"total_tokens": 12}}

    async def fake_stream():
        yield chunk1
        yield chunk2

    mock_client.chat.completions.create = AsyncMock(return_value=fake_stream())

    adapter = GroqAdapter(api_key="test-key", default_model="llama-3.3-70b", client=mock_client)
    req = CompletionRequest(messages=[{"role": "user", "content": "Hi"}])

    chunks = []
    async for c in adapter.stream(req):
        chunks.append(c)

    assert len(chunks) == 2
    assert chunks[0].delta == "Hello"
    assert chunks[1].delta == " world"
    assert chunks[1].finish_reason == "stop"
    assert chunks[1].usage == {"total_tokens": 12}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raised_exc", "expected_err"),
    [
        (APIConnectionError(request=MagicMock()), ProviderUnavailableError),
        (
            RateLimitError(message="Rate limited", response=MagicMock(status_code=429), body=None),
            ProviderRateLimitError,
        ),
        (
            APIStatusError(message="Unauthorized", response=MagicMock(status_code=401), body=None),
            ProviderAuthenticationError,
        ),
        (
            APIStatusError(
                message="Context length exceeded: 8000 tokens",
                response=MagicMock(status_code=400),
                body=None,
            ),
            ContextWindowExceededError,
        ),
        (
            APIStatusError(
                message="Invalid parameter", response=MagicMock(status_code=400), body=None
            ),
            ProviderInvalidRequestError,
        ),
    ],
)
async def test_groq_adapter_error_mapping(raised_exc, expected_err):
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(side_effect=raised_exc)

    adapter = GroqAdapter(api_key="test-key", default_model="llama-3.3-70b", client=mock_client)
    req = CompletionRequest(messages=[{"role": "user", "content": "Hi"}])

    with pytest.raises(expected_err):
        await adapter.complete(req)


# --- OllamaAdapter tests ---


@pytest.mark.asyncio
async def test_ollama_adapter_complete_success():
    def handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        assert data["model"] == "llama3.2"
        assert data["options"]["temperature"] == 0.2
        assert data["options"]["num_predict"] == 50

        response_body = {
            "message": {
                "role": "assistant",
                "content": "Ollama response",
                "tool_calls": [
                    {
                        "function": {
                            "name": "lookup",
                            "arguments": {"term": "clean architecture"},
                        }
                    }
                ],
            },
            "prompt_eval_count": 8,
            "eval_count": 12,
        }
        return httpx.Response(200, json=response_body)

    client = httpx.AsyncClient(
        base_url="http://localhost:11434", transport=httpx.MockTransport(handler)
    )
    adapter = OllamaAdapter(
        base_url="http://localhost:11434", default_model="llama3.2", client=client
    )

    req = CompletionRequest(
        messages=[{"role": "user", "content": "Search"}],
        model_options=ModelOptions(temperature=0.2, max_tokens=50),
    )

    resp = await adapter.complete(req)
    assert resp.content == "Ollama response"
    assert resp.tool_calls is not None
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "lookup"
    assert json.loads(resp.tool_calls[0].arguments) == {"term": "clean architecture"}
    assert resp.usage == {"prompt_tokens": 8, "completion_tokens": 12, "total_tokens": 20}


@pytest.mark.asyncio
async def test_ollama_adapter_stream_success():
    lines = [
        json.dumps({"message": {"content": "Hello"}, "done": False}) + "\n",
        json.dumps(
            {
                "message": {"content": " world"},
                "done": True,
                "prompt_eval_count": 5,
                "eval_count": 7,
            }
        )
        + "\n",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content="".join(lines).encode("utf-8"))

    client = httpx.AsyncClient(
        base_url="http://localhost:11434", transport=httpx.MockTransport(handler)
    )
    adapter = OllamaAdapter(
        base_url="http://localhost:11434", default_model="llama3.2", client=client
    )

    req = CompletionRequest(messages=[{"role": "user", "content": "Hi"}])
    chunks = []
    async for c in adapter.stream(req):
        chunks.append(c)

    assert len(chunks) == 2
    assert chunks[0].delta == "Hello"
    assert chunks[1].delta == " world"
    assert chunks[1].finish_reason == "stop"
    assert chunks[1].usage == {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}


@pytest.mark.asyncio
async def test_ollama_adapter_error_mapping():
    # Test connection error
    def conn_error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    client1 = httpx.AsyncClient(
        base_url="http://localhost:11434", transport=httpx.MockTransport(conn_error_handler)
    )
    adapter1 = OllamaAdapter(base_url="http://localhost:11434", client=client1)
    req = CompletionRequest(messages=[{"role": "user", "content": "Hi"}])

    with pytest.raises(ProviderUnavailableError):
        await adapter1.complete(req)

    # Test rate limit error (429)
    def rate_limit_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Too many requests")

    client2 = httpx.AsyncClient(
        base_url="http://localhost:11434", transport=httpx.MockTransport(rate_limit_handler)
    )
    adapter2 = OllamaAdapter(base_url="http://localhost:11434", client=client2)

    with pytest.raises(ProviderRateLimitError):
        await adapter2.complete(req)

    # Test bad request error (400)
    def bad_request_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="Bad json")

    client3 = httpx.AsyncClient(
        base_url="http://localhost:11434", transport=httpx.MockTransport(bad_request_handler)
    )
    adapter3 = OllamaAdapter(base_url="http://localhost:11434", client=client3)

    with pytest.raises(ProviderInvalidRequestError):
        await adapter3.complete(req)
