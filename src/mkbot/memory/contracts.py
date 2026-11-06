from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from mkbot.domain.models import StrictModel, utc

MemoryKind = Literal[
    "fact", "preference", "boundary", "current_state", "experience", "opinion", "inference"
]


def valid_scope(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    if not value or any(not s or len(s) > 200 for s in value):
        raise ValueError("记忆范围需要明确的会话 ID、@self 或 *")
    return ["*"] if "*" in value else sorted(set(value))


class MemorySettings(StrictModel):
    # 普通 FIFO 区和保护区分别限容；总近期容量是两者之和。
    recent_capacity: int = Field(default=200, ge=1, le=10000)
    recent_per_scope: int = Field(default=32, ge=1, le=1000)
    protected_capacity: int = Field(default=40, ge=0, le=2000)
    protected_per_scope: int = Field(default=8, ge=0, le=500)
    protected_hours: float = Field(default=24, gt=0, le=720)
    half_life_hours: float = Field(default=72, gt=0, le=8760)
    activation_cap: float = Field(default=5, gt=0, le=100)
    use_reward: float = Field(default=1, gt=0, le=10)
    daily_reward_limit: float = Field(default=2, ge=0, le=100)
    context_max_items: int = Field(default=12, ge=1, le=100)
    context_max_chars: int = Field(default=12000, ge=100, le=50000)
    share_group_memories: bool = True


class MemoryProposal(StrictModel):
    kind: MemoryKind
    subjects: list[str] = Field(min_length=1, max_length=20)
    content: str = Field(min_length=1, max_length=8000)
    tier: Literal["recent", "long"] = "recent"
    source_refs: list[str] = Field(min_length=1, max_length=50)
    use_scope: list[str] | None = None
    disclosure_scope: list[str] | None = None
    importance: float = Field(default=0.5, ge=0, le=1)
    valid_until: datetime | None = None
    supersedes: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)

    _scope = field_validator("use_scope", "disclosure_scope")(valid_scope)

    @field_validator("subjects", "source_refs")
    @classmethod
    def unique_ids(cls, values):
        if any(not value or len(value) > 250 for value in values):
            raise ValueError("人物与来源 ID 不能为空或过长")
        return sorted(set(values))

    @field_validator("valid_until")
    @classmethod
    def aware(cls, value):
        return utc(value) if value else None

    @model_validator(mode="after")
    def complete_revision(self):
        if self.kind == "current_state" and self.valid_until is None:
            raise ValueError("临时近况需要有效期")
        if bool(self.supersedes) != (self.expected_revision is not None):
            raise ValueError("替代记忆时需要旧记录的版本号")
        return self


class MemoryEdit(StrictModel):
    expected_revision: int = Field(ge=1)
    content: str | None = Field(default=None, min_length=1, max_length=8000)
    importance: float | None = Field(default=None, ge=0, le=1)
    tier: Literal["recent", "long"] | None = None
    use_scope: list[str] | None = None
    disclosure_scope: list[str] | None = None
    valid_until: datetime | None = None
    allow_wider_sharing: bool = False

    _scope = field_validator("use_scope", "disclosure_scope")(valid_scope)

    @field_validator("valid_until")
    @classmethod
    def aware(cls, value):
        return utc(value) if value else None

    @model_validator(mode="after")
    def explicit_nulls(self):
        for field in self.model_fields_set - {"valid_until"}:
            if getattr(self, field) is None:
                raise ValueError("只有 valid_until 可以显式清空")
        return self


class ManualMemory(StrictModel):
    """管理者录入是独立的人工来源，不能伪装成用户原话。"""

    kind: MemoryKind
    subjects: list[str] = Field(min_length=1, max_length=20)
    content: str = Field(min_length=1, max_length=8000)
    tier: Literal["recent", "long"] = "recent"
    scope: list[str]
    importance: float = Field(default=0.5, ge=0, le=1)
    valid_until: datetime | None = None

    _scope = field_validator("scope")(valid_scope)

    @field_validator("subjects")
    @classmethod
    def unique_ids(cls, values):
        return MemoryProposal.unique_ids(values)

    @field_validator("valid_until")
    @classmethod
    def aware(cls, value):
        return utc(value) if value else None

    @model_validator(mode="after")
    def temporary_expiry(self):
        if self.kind == "current_state" and self.valid_until is None:
            raise ValueError("临时近况需要有效期")
        return self


class MemoryRef(StrictModel):
    id: str
    revision: int = Field(ge=1)
