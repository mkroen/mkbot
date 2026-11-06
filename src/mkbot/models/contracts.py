"""供应商适配器只完成一次请求；重试与预算归 Gateway 所有。"""

from decimal import ROUND_CEILING, Decimal
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from mkbot.domain.errors import MkbotError
from mkbot.domain.models import StrictModel


class ToolCall(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any]


class Message(StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(default="", max_length=200_000)
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=16)
    reasoning_content: str | None = Field(default=None, max_length=200_000)


class ToolDefinition(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=4000)
    parameters: dict[str, Any]


class ModelRequest(StrictModel):
    model: str
    messages: list[Message] = Field(min_length=1, max_length=200)
    tools: list[ToolDefinition] = Field(default_factory=list, max_length=50)
    max_output_tokens: int = Field(default=1024, ge=1, le=32768)
    parameters: dict[str, Any] = Field(default_factory=dict)


class Usage(StrictModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid_cache(self):
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("缓存输入不能超过总输入")
        return self


class ModelReply(StrictModel):
    text: str = Field(default="", max_length=200_000)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=16)
    usage: Usage | None = None
    served_model: str | None = None
    finish_reason: str | None = None
    reasoning_content: str | None = Field(default=None, max_length=200_000)


class Prices(StrictModel):
    """每百万 token 的美元价；结果用整数 micro-USD 存储。"""

    input_usd_per_million: Decimal = Field(ge=0, le=10000)
    output_usd_per_million: Decimal = Field(ge=0, le=10000)
    cached_input_usd_per_million: Decimal | None = Field(default=None, ge=0, le=10000)

    def cost(self, usage: Usage) -> int:
        cached = self.cached_input_usd_per_million
        value = (
            (usage.input_tokens - usage.cached_input_tokens) * self.input_usd_per_million
            + usage.cached_input_tokens
            * (cached if cached is not None else self.input_usd_per_million)
            + usage.output_tokens * self.output_usd_per_million
        )
        return int(value.to_integral_value(rounding=ROUND_CEILING))


class Route(StrictModel):
    provider: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=200)
    prices: Prices
    parameters: dict[str, Any] = Field(default_factory=dict)


class Endpoint(StrictModel):
    base_url: str
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,99}$")
    token_limit_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"

    @field_validator("base_url")
    @classmethod
    def safe_endpoint(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("模型地址须为 http(s) 基础地址，不含凭据、查询参数或片段")
        return value.rstrip("/")


class GatewaySettings(StrictModel):
    providers: dict[str, Endpoint] = Field(default_factory=dict)
    roles: dict[str, list[Route]] = Field(default_factory=dict)
    max_attempts: int = Field(default=3, ge=1, le=6)
    timeout_seconds: float = Field(default=180, gt=0, le=600)
    max_concurrency: int = Field(default=1, ge=1, le=16)
    daily_limit_micro_usd: int = Field(default=1_000_000, ge=0)
    root_limit_micro_usd: int = Field(default=100_000, ge=0)
    root_call_limit: int = Field(default=12, ge=1, le=100)
    circuit_threshold: int = Field(default=3, ge=1, le=20)
    circuit_cooldown_seconds: int = Field(default=60, ge=1, le=3600)

    @model_validator(mode="after")
    def bounded_routes(self):
        if any(not routes or len(routes) > 6 for routes in self.roles.values()):
            raise ValueError("每种模型角色需要 1 至 6 条路由")
        return self


class ProviderError(Exception):
    def __init__(
        self,
        kind: Literal[
            "rate_limit",
            "quota",
            "unavailable",
            "timeout",
            "auth",
            "invalid_request",
            "invalid_response",
        ],
        *,
        usage: Usage | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        # 供应商返回正文可能包含密钥或私聊内容，不直接写入日志。
        super().__init__(kind)
        self.kind, self.usage, self.retry_after_seconds = kind, usage, retry_after_seconds


class ModelUnavailable(MkbotError):
    def __init__(self, reason: str, *, retry_at: str | None = None) -> None:
        super().__init__(reason)
        self.retry_at = retry_at


class Provider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelReply:
        """只发送一次请求，不做 SDK 隐式重试。"""
        ...
