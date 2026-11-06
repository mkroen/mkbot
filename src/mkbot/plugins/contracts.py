"""插件提供能力描述、专业提示词与类型化入口，主会话自行决定何时使用。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field

from mkbot.domain.errors import MkbotError
from mkbot.domain.models import StrictModel


class Manifest(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,39}$")
    name: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=3000)
    instructions: str = Field(default="", max_length=12000)


class ToolResult(StrictModel):
    ok: bool = True
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = Field(default=None, max_length=2000)


class ToolOutcomeUnknown(MkbotError):
    pass


class ToolRejected(Exception):
    """插件已确认此次操作没有外部副作用时使用。"""


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    arguments: type[StrictModel]
    handler: Callable[[Any, StrictModel], Awaitable[ToolResult]]
    effect: Literal["read_only", "local", "external"] = "read_only"
    timeout_seconds: float = 30
    evidence_kind: Literal["none", "observation", "artifact"] = "none"


@dataclass(frozen=True, slots=True)
class Plugin:
    manifest: Manifest
    tools: tuple[Tool, ...]
