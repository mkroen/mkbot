"""调度参数与角色可提交的终止决策；执行约束不来自提示词。"""

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from mkbot.domain.models import StrictModel, utc


class SchedulerSettings(StrictModel):
    enabled: bool = False
    chat_enabled: bool = True
    autonomous_enabled: bool = True
    run_plans: bool = True
    debounce_seconds: float = Field(default=2, ge=0, le=30)
    max_batch_wait_seconds: float = Field(default=10, ge=1, le=120)
    max_batch_events: int = Field(default=80, ge=1, le=200)
    conversation_cooldown_seconds: float = Field(default=10, ge=0, le=600)
    dispatch_interval_seconds: float = Field(default=1, ge=0.1, le=60)
    max_message_age_hours: float = Field(default=48, gt=0, le=720)
    max_deferrals: int = Field(default=2, ge=0, le=10)
    minimum_defer_seconds: int = Field(default=60, ge=1, le=86400)
    idle_interval_seconds: int = Field(default=1800, ge=60, le=86400)
    idle_max_runs_per_day: int = Field(default=24, ge=0, le=1000)
    max_chat_burst: int = Field(default=4, ge=1, le=100)
    max_open_plans: int = Field(default=20, ge=1, le=200)
    max_plans_per_root: int = Field(default=4, ge=1, le=20)
    max_runs_per_plan: int = Field(default=8, ge=1, le=100)
    minimum_plan_interval_seconds: int = Field(default=60, ge=1, le=86400)
    idle_goal: str = Field(
        default="根据自己的兴趣、经历和未完成计划，选择现在想做的事。"
        "可以使用技能，也可以等待或休息；没有新进展时不要反复自问自答。",
        min_length=1,
        max_length=4000,
    )


class ChatChoice(StrictModel):
    decision: Literal["reply", "no_reply", "defer", "expired"]
    reason: str = Field(min_length=1, max_length=500)
    text: str = Field(default="", max_length=8000)
    revisit_at: datetime | None = None

    @field_validator("revisit_at")
    @classmethod
    def aware(cls, value):
        return utc(value) if value else None

    @model_validator(mode="after")
    def consistent(self):
        if self.decision == "reply" and not self.text.strip():
            raise ValueError("选择回复时需要填写回复内容")
        if self.decision != "reply" and self.text:
            raise ValueError("不回复的决定不能附带发送内容")
        if self.decision == "defer" and self.revisit_at is None:
            raise ValueError("暂缓必须给出明确的再次评估时间")
        if self.decision != "defer" and self.revisit_at is not None:
            raise ValueError("只有暂缓决定可以设置再次评估时间")
        return self


class IdleWait(StrictModel):
    until: datetime
    reason: str = Field(min_length=1, max_length=500)

    _utc = field_validator("until")(utc)
