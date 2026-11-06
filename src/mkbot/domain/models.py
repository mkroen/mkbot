"""消息、活动与生命周期的边界类型。时间统一使用带时区的 UTC。"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Phase(StrEnum):
    AWAKE = "awake"
    WINDING_DOWN = "winding_down"
    SLEEPING = "sleeping"
    WAKING = "waking"


class ActivityStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(UTC)


class InboundEvent(StrictModel):
    source: str = Field(min_length=1, max_length=80)
    external_id: str = Field(min_length=1, max_length=200)
    conversation_id: str = Field(min_length=1, max_length=200)
    sender_id: str = Field(min_length=1, max_length=120)
    conversation_kind: Literal["private", "group", "system"] = "private"
    text: str = Field(default="", max_length=100_000)
    directed: bool = False
    occurred_at: datetime
    attachments: list[dict[str, Any]] = Field(default_factory=list, max_length=100)

    _utc = field_validator("occurred_at")(utc)


class SleepRequest(StrictModel):
    earliest_wake_at: datetime
    latest_wake_at: datetime
    reason: str = Field(default="准备休息", max_length=500)

    _utc = field_validator("earliest_wake_at", "latest_wake_at")(utc)

    @model_validator(mode="after")
    def ordered_window(self) -> "SleepRequest":
        if self.latest_wake_at < self.earliest_wake_at:
            raise ValueError("最晚唤醒时间不能早于最早唤醒时间")
        return self


class ActivityRequest(StrictModel):
    kind: str = Field(min_length=1, max_length=80)
    goal: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=200)
    origin: Literal["user", "autonomous", "maintenance", "operator"] = "operator"
    priority: int = Field(default=0, ge=-100, le=100)
    due_at: datetime | None = None
    parent_id: str | None = None

    @field_validator("due_at")
    @classmethod
    def aware_due(cls, value: datetime | None) -> datetime | None:
        return utc(value) if value else None


class ReviewDecision(StrictModel):
    event_ids: list[str] = Field(min_length=1, max_length=200)
    decision: Literal["reply", "no_reply", "defer", "expired"]
    reason: str = Field(min_length=1, max_length=1000)
    revisit_at: datetime | None = None

    @field_validator("revisit_at")
    @classmethod
    def aware_revisit(cls, value: datetime | None) -> datetime | None:
        return utc(value) if value else None

    @model_validator(mode="after")
    def deferred_has_trigger(self) -> "ReviewDecision":
        if self.decision == "defer" and self.revisit_at is None:
            raise ValueError("暂缓处理必须指定再评估时间")
        return self
