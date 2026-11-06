"""Chat Completions 协议适配；无隐藏重试、重定向或系统代理继承。"""

import json
import os

import httpx

from mkbot.models.contracts import (
    Endpoint,
    ModelReply,
    ModelRequest,
    ProviderError,
    ToolCall,
    Usage,
)
from mkbot.storage.database import encode

ALLOWED_PARAMETERS = {
    "temperature",
    "top_p",
    "seed",
    "frequency_penalty",
    "presence_penalty",
    "stop",
    "response_format",
    "reasoning_effort",
    "thinking",
}


class ChatCompletionProvider:
    def __init__(
        self, endpoint: Endpoint, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.endpoint, self.transport = endpoint, transport

    async def complete(self, request: ModelRequest) -> ModelReply:
        if set(request.parameters) - ALLOWED_PARAMETERS:
            raise ProviderError("invalid_request", usage=Usage(input_tokens=0, output_tokens=0))
        headers = {}
        if self.endpoint.api_key_env:
            secret = os.environ.get(self.endpoint.api_key_env)
            if not secret:
                raise ProviderError("auth", usage=Usage(input_tokens=0, output_tokens=0))
            headers["Authorization"] = f"Bearer {secret}"
        messages = []
        for message in request.messages:
            item = {"role": message.role, "content": message.content}
            if message.role == "tool":
                if not message.tool_call_id:
                    raise ProviderError(
                        "invalid_request", usage=Usage(input_tokens=0, output_tokens=0)
                    )
                item["tool_call_id"] = message.tool_call_id
            if message.role == "assistant":
                if message.reasoning_content is not None:
                    item["reasoning_content"] = message.reasoning_content
                if message.tool_calls:
                    item["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": encode(call.arguments)},
                        }
                        for call in message.tool_calls
                    ]
            messages.append(item)
        payload = request.parameters | {
            "model": request.model,
            "messages": messages,
            "stream": False,
            self.endpoint.token_limit_field: request.max_output_tokens,
        }
        if request.tools:
            payload["tools"] = [
                {"type": "function", "function": tool.model_dump()} for tool in request.tools
            ]
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=None, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.post(
                    self.endpoint.base_url + "/chat/completions", json=payload, headers=headers
                )
        except httpx.TimeoutException:
            raise ProviderError("timeout") from None
        except httpx.RequestError:
            raise ProviderError("unavailable") from None
        if not response.is_success:
            kind = {401: "auth", 403: "auth", 402: "quota", 408: "timeout", 429: "rate_limit"}.get(
                response.status_code,
                "unavailable" if response.status_code >= 500 else "invalid_request",
            )
            retry_after = response.headers.get("Retry-After", "")
            # 错误响应无 usage 时依然保留预估费用；不能假定请求免费。
            raise ProviderError(
                kind,
                retry_after_seconds=min(int(retry_after), 3600) if retry_after.isdigit() else None,
            )
        usage = None
        try:
            body = response.json()
            raw_usage = body.get("usage")
            if raw_usage and "prompt_tokens" in raw_usage and "completion_tokens" in raw_usage:
                cached = (raw_usage.get("prompt_tokens_details") or {}).get(
                    "cached_tokens", raw_usage.get("prompt_cache_hit_tokens", 0)
                )
                usage = Usage(
                    input_tokens=raw_usage["prompt_tokens"],
                    output_tokens=raw_usage["completion_tokens"],
                    cached_input_tokens=cached,
                )
            choice = body["choices"][0]
            message = choice["message"]
            calls = [
                ToolCall(
                    id=call["id"],
                    name=call["function"]["name"],
                    arguments=json.loads(call["function"]["arguments"]),
                )
                for call in (message.get("tool_calls") or [])
            ]
            return ModelReply(
                text=message.get("content") or "",
                tool_calls=calls,
                usage=usage,
                served_model=body.get("model"),
                finish_reason=choice.get("finish_reason"),
                reasoning_content=message.get("reasoning_content"),
            )
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError("invalid_response", usage=usage) from None
