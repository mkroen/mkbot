import asyncio
import sqlite3
from datetime import timedelta

import pytest

from mkbot.domain.errors import AdmissionDenied, BudgetExceeded, Conflict
from mkbot.domain.models import ActivityRequest, SleepRequest
from mkbot.models.contracts import Message, ModelReply, ModelUnavailable, ProviderError, Usage
from mkbot.runtime.kernel import Kernel
from mkbot.storage.database import SCHEMA, SCHEMA_VERSION, Database

MESSAGES = [Message(role="user", content="你好")]
PRICES = {"input_usd_per_million": "1", "output_usd_per_million": "2"}


class ScriptedProvider:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        result = self.steps.pop(0)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return await result()
        return result


def configured(kernel, primary, secondary=None, **options):
    snapshot = kernel.config.get("models")
    routes = [{"provider": "primary", "model": "sample-a", "prices": PRICES}]
    kernel.models.register("primary", primary)
    if secondary:
        routes.append({"provider": "secondary", "model": "sample-b", "prices": PRICES})
        kernel.models.register("secondary", secondary)
    content = snapshot["content"] | {"roles": {"planner": routes}} | options
    kernel.config.put("models", content, snapshot["revision"])
    item = kernel.activities.create(ActivityRequest(kind="chat", goal="测试一次模型调用"))
    kernel.activities.claim(item["id"])
    return item["id"]


def answer(text="收到"):
    return ModelReply(text=text, usage=Usage(input_tokens=10, output_tokens=5))


async def test_same_request_is_reused_and_all_usage_is_accounted(kernel):
    provider = ScriptedProvider(answer())
    activity = configured(kernel, provider)
    first = await kernel.models.call(activity, "decision/0", "planner", MESSAGES)
    second = await kernel.models.call(activity, "decision/0", "planner", MESSAGES)
    assert first == second
    assert len(provider.requests) == 1
    assert kernel.models.calls()[0]["route_revision"] == 2
    assert kernel.models.calls()[0]["calculated_micro_usd"] == 20
    assert next(x for x in kernel.budgets.usage() if x["scope"].startswith("day:"))["settled"] == 20
    with pytest.raises(Conflict):
        await kernel.models.call(
            activity, "decision/0", "planner", [Message(role="user", content="不同")]
        )


async def test_fallback_accounts_for_failed_attempt_without_reset(kernel):
    primary = ScriptedProvider(ProviderError("unavailable"))
    secondary = ScriptedProvider(answer())
    activity = configured(kernel, primary, secondary)
    result = await kernel.models.call(activity, "one", "planner", MESSAGES)
    assert result.text == "收到"
    calls = kernel.models.calls()
    assert {c["status"] for c in calls} == {"succeeded", "failed"}
    assert {c["attempt"] for c in calls} == {1, 2}
    assert any(x["reserved"] > 0 for x in kernel.budgets.usage())
    assert len(primary.requests) == len(secondary.requests) == 1


async def test_same_request_backoff_and_attempt_limit_survive_restart(tmp_path, clock):
    directory = tmp_path / "durable-model"
    kernel = Kernel(directory, clock)
    provider = ScriptedProvider(ProviderError("unavailable"))
    activity = configured(kernel, provider, max_attempts=2)
    with pytest.raises(ModelUnavailable) as first:
        await kernel.models.call(activity, "one", "planner", MESSAGES)
    assert first.value.retry_at
    with pytest.raises(ModelUnavailable):
        await kernel.models.call(activity, "one", "planner", MESSAGES)
    assert len(provider.requests) == 1
    kernel.activities.transition(activity, "waiting")
    kernel.close()
    clock.advance(seconds=11)
    recovered = Kernel(directory, clock)
    try:
        provider = ScriptedProvider(ProviderError("unavailable"))
        recovered.models.register("primary", provider)
        recovered.activities.claim(activity)
        with pytest.raises(ModelUnavailable):
            await recovered.models.call(activity, "one", "planner", MESSAGES)
        clock.advance(seconds=11)
        with pytest.raises(ModelUnavailable, match="上限"):
            await recovered.models.call(activity, "one", "planner", MESSAGES)
        assert len(provider.requests) == 1
        assert len(recovered.models.calls()) == 2
    finally:
        recovered.close()


async def test_sleep_does_not_make_provider_request(kernel, clock):
    provider = ScriptedProvider(answer())
    activity = configured(kernel, provider)
    kernel.activities.transition(activity, "paused")
    wake = clock.now() + timedelta(hours=1)
    kernel.lifecycle.request_sleep(SleepRequest(earliest_wake_at=wake, latest_wake_at=wake))
    with pytest.raises(AdmissionDenied):
        await kernel.models.call(activity, "one", "planner", MESSAGES)
    assert provider.requests == []
    assert kernel.models.calls() == []


async def test_cancelled_model_keeps_unknown_usage_and_blocks_reissue(kernel):
    entered = asyncio.Event()

    async def wait_forever():
        entered.set()
        await asyncio.Event().wait()

    provider = ScriptedProvider(wait_forever)
    activity = configured(kernel, provider)
    task = asyncio.create_task(kernel.models.call(activity, "one", "planner", MESSAGES))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert kernel.models.calls()[0]["status"] == "unknown"
    assert kernel.lifecycle.state()["work"][0]["status"] == "unknown"
    with pytest.raises(ModelUnavailable):
        await kernel.models.call(activity, "one", "planner", MESSAGES)
    assert len(provider.requests) == 1


async def test_call_and_money_limits_apply_before_network(kernel):
    provider = ScriptedProvider(answer(), answer())
    activity = configured(kernel, provider, root_call_limit=1)
    await kernel.models.call(activity, "one", "planner", MESSAGES)
    with pytest.raises(BudgetExceeded):
        await kernel.models.call(activity, "two", "planner", MESSAGES)
    assert len(provider.requests) == 1
    assert kernel.lifecycle.state()["work"] == []


async def test_circuit_is_shared_across_independent_activities(kernel, clock):
    provider = ScriptedProvider(ProviderError("quota"), answer())
    activity = configured(kernel, provider)
    with pytest.raises(ModelUnavailable):
        await kernel.models.call(activity, "one", "planner", MESSAGES)
    other = kernel.activities.create(ActivityRequest(kind="chat", goal="另一件事"))["id"]
    kernel.activities.claim(other)
    with pytest.raises(ModelUnavailable):
        await kernel.models.call(other, "new", "planner", MESSAGES)
    assert len(provider.requests) == 1
    clock.advance(seconds=61)
    assert (await kernel.models.call(other, "new", "planner", MESSAGES)).text == "收到"


async def test_usage_unknown_is_not_settled_as_zero_and_price_snapshot_is_stable(kernel):
    provider = ScriptedProvider(ModelReply(text="无用量"))
    activity = configured(kernel, provider)
    await kernel.models.call(activity, "one", "planner", MESSAGES)
    snapshot = kernel.config.get("models")
    content = snapshot["content"]
    content["roles"]["planner"][0]["prices"]["input_usd_per_million"] = "9"
    kernel.config.put("models", content, snapshot["revision"])
    call = kernel.models.calls()[0]
    assert call["usage"] is None
    assert call["calculated_micro_usd"] is None
    assert call["estimated_micro_usd"] > 0
    assert call["prices"]["input_usd_per_million"] == "1"
    assert all(x["reserved"] > 0 for x in kernel.budgets.usage())


def test_version_one_database_migrates_without_dropping_existing_data(tmp_path):
    path = tmp_path / "v1.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA + "PRAGMA user_version=1;")
    connection.execute("INSERT INTO config_revisions VALUES('existing',1,'{}','2026-11-06')")
    connection.commit()
    connection.close()
    db = Database(path)
    try:
        assert db.rows("SELECT name FROM config_revisions")[0]["name"] == "existing"
        assert db.rows("SELECT * FROM model_calls") == []
        assert db.rows("PRAGMA user_version")[0]["user_version"] == SCHEMA_VERSION
    finally:
        db.close()
