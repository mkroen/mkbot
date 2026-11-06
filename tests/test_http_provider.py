import json

import httpx
import pytest

from mkbot.models.contracts import (
    Endpoint,
    Message,
    ModelRequest,
    ProviderError,
    ToolCall,
    ToolDefinition,
)
from mkbot.models.http_chat import ChatCompletionProvider


async def test_http_protocol_preserves_tool_context_and_cache_usage(monkeypatch):
    monkeypatch.setenv("MKBOT_TEST_KEY", "test-credential-not-production")
    sent = []

    def handler(request):
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-credential-not-production"
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "served-model",
                "choices": [{"message": {"content": "已经好了"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 40},
                },
            },
        )

    adapter = ChatCompletionProvider(
        Endpoint(base_url="https://model.example/v1", api_key_env="MKBOT_TEST_KEY"),
        transport=httpx.MockTransport(handler),
    )
    result = await adapter.complete(
        ModelRequest(
            model="requested-model",
            messages=[
                Message(role="user", content="记个笔记"),
                Message(
                    role="assistant",
                    reasoning_content="test-only-provider-context",
                    tool_calls=[ToolCall(id="call1", name="note", arguments={"text": "内容"})],
                ),
                Message(role="tool", tool_call_id="call1", content='{"ok":true}'),
            ],
            tools=[
                ToolDefinition(name="note", description="保存笔记", parameters={"type": "object"})
            ],
        )
    )
    assert result.served_model == "served-model"
    assert result.usage.cached_input_tokens == 40
    assert sent[0]["stream"] is False
    assert sent[0]["messages"][1]["reasoning_content"] == "test-only-provider-context"
    assert (
        json.loads(sent[0]["messages"][1]["tool_calls"][0]["function"]["arguments"])["text"]
        == "内容"
    )
    assert len(sent) == 1


@pytest.mark.parametrize(
    "status,kind",
    [
        (401, "auth"),
        (402, "quota"),
        (429, "rate_limit"),
        (503, "unavailable"),
        (400, "invalid_request"),
    ],
)
async def test_http_status_classification_has_no_adapter_retry(status, kind):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "22"}, text="private upstream body")

    adapter = ChatCompletionProvider(
        Endpoint(base_url="https://model.example"), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ProviderError) as exc:
        await adapter.complete(
            ModelRequest(model="any", messages=[Message(role="user", content="test")])
        )
    assert exc.value.kind == kind
    assert exc.value.retry_after_seconds == 22
    assert exc.value.usage is None
    assert "private upstream body" not in str(exc.value)
    assert len(calls) == 1


async def test_usage_missing_is_not_zero_and_redirect_is_not_followed():
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": "reply"}}]})
        return httpx.Response(302, headers={"Location": "https://elsewhere.example"})

    adapter = ChatCompletionProvider(
        Endpoint(base_url="https://model.example"), transport=httpx.MockTransport(handler)
    )
    request = ModelRequest(model="any", messages=[Message(role="user", content="test")])
    assert (await adapter.complete(request)).usage is None
    with pytest.raises(ProviderError):
        await adapter.complete(request)
    assert count == 2


async def test_bad_tool_json_keeps_known_usage_and_stops_result():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"id": "c", "function": {"name": "note", "arguments": "not-json"}},
                            ]
                        }
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5},
            },
        )

    adapter = ChatCompletionProvider(
        Endpoint(base_url="https://model.example"), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ProviderError) as exc:
        await adapter.complete(
            ModelRequest(model="any", messages=[Message(role="user", content="test")])
        )
    assert exc.value.kind == "invalid_response"
    assert exc.value.usage.output_tokens == 5


async def test_route_parameters_cannot_override_messages_or_model():
    calls = []
    adapter = ChatCompletionProvider(
        Endpoint(base_url="https://model.example"),
        transport=httpx.MockTransport(lambda req: calls.append(req)),
    )
    with pytest.raises(ProviderError, match="invalid_request"):
        await adapter.complete(
            ModelRequest(
                model="any",
                messages=[Message(role="user", content="test")],
                parameters={"messages": []},
            )
        )
    assert not calls


def test_credentials_must_not_be_embedded_in_base_url():
    with pytest.raises(ValueError):
        Endpoint(base_url="https://user:secret@model.example")
    with pytest.raises(ValueError):
        Endpoint(base_url="https://model.example?api_key=secret")
