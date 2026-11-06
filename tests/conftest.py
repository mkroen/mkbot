from datetime import UTC, datetime

import pytest

from mkbot.runtime.clock import ManualClock
from mkbot.runtime.kernel import Kernel


@pytest.fixture
def clock():
    return ManualClock(datetime(2026, 11, 6, 1, tzinfo=UTC))


@pytest.fixture
def kernel(tmp_path, clock):
    instance = Kernel(tmp_path / "instance", clock)
    yield instance
    instance.close()
