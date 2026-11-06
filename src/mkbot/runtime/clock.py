"""时间抽象使睡眠、过期和重启场景可用虚拟时钟验证。"""

from datetime import UTC, datetime, timedelta
from typing import Protocol

from mkbot.domain.models import utc


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    def __init__(self, initial: datetime) -> None:
        self._now = utc(initial)

    def now(self) -> datetime:
        return self._now

    def advance(self, *, seconds: float = 0) -> datetime:
        if seconds < 0:
            raise ValueError("虚拟时钟不能倒退")
        self._now += timedelta(seconds=seconds)
        return self._now


def timestamp(value: datetime) -> str:
    return utc(value).isoformat(timespec="microseconds")
