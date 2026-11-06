"""长期目标保存在活动中；计划只补充进度，不另立一套任务状态。"""

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from mkbot.domain.models import StrictModel, utc


class PlanProposal(StrictModel):
    goal: str = Field(min_length=1, max_length=2000)
    next_step: str = Field(min_length=1, max_length=2000)
    due_at: datetime | None = None

    _utc = field_validator("due_at")(lambda value: utc(value) if value else None)

    @field_validator("goal")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("目标不能为空白")
        return value.strip()

    @model_validator(mode="after")
    def next_action(self):
        if getattr(self, "status", "waiting") == "waiting" and not self.next_step.strip():
            raise ValueError("待继续的目标需要明确下一步")
        return self


class ManualPlan(PlanProposal):
    conversation_id: str | None = Field(default=None, min_length=1, max_length=200)


class PlanEdit(PlanProposal):
    next_step: str = Field(default="", max_length=2000)
    expected_version: int = Field(ge=1)
    progress: str = Field(default="", max_length=4000)
    status: Literal["waiting", "paused", "completed", "cancelled"] = "waiting"

    @model_validator(mode="after")
    def consistent(self):
        if self.status != "waiting" and self.due_at is not None:
            raise ValueError("只有待执行的目标可以设置下次时间")
        return self


class PlanOutcome(StrictModel):
    decision: Literal["continue", "pause", "completed", "cancelled"]
    result: str = Field(min_length=1, max_length=2000)
    progress: str = Field(min_length=1, max_length=4000)
    next_step: str = Field(default="", max_length=2000)
    due_at: datetime | None = None

    _utc = field_validator("due_at")(lambda value: utc(value) if value else None)

    @model_validator(mode="after")
    def consistent(self):
        if not self.result.strip() or not self.progress.strip():
            raise ValueError("请说明本轮实际结果和累计进度")
        if self.decision == "continue":
            if self.due_at is None or not self.next_step.strip():
                raise ValueError("继续目标需要明确的下一步和执行时间")
        elif self.due_at is not None:
            raise ValueError("仅继续目标可以设置执行时间")
        return self
