import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_model_gateway import PRICES, ScriptedProvider, answer
from test_plugins_agent import note_call

from mkbot.domain.models import ActivityRequest, InboundEvent, ReviewDecision, SleepRequest
from mkbot.models.contracts import ModelReply, ProviderError, ToolCall, Usage
from mkbot.runtime.kernel import Kernel


def setting(kernel, name, **changes):
    saved = kernel.config.get(name)
    kernel.config.put(name, saved["content"] | changes, saved["revision"])


def enable(kernel, provider, **changes):
    kernel.models.register("primary", provider)
    setting(
        kernel,
        "models",
        roles={"planner": [{"provider": "primary", "model": "scripted", "prices": PRICES}]},
    )
    setting(kernel, "scheduler", enabled=True, autonomous_enabled=False, **changes)


def receive(kernel, text="你好", conversation="private:alice", **kwargs):
    return kernel.inbox.receive(
        InboundEvent(
            source="test",
            external_id=str(uuid4()),
            conversation_id=conversation,
            sender_id="alice",
            text=text,
            occurred_at=kernel.clock.now(),
            **kwargs,
        )
    )["id"]


def choice(decision="no_reply", **kwargs):
    return ModelReply(
        tool_calls=[
            ToolCall(
                id=str(uuid4()),
                name="life__review",
                arguments={"decision": decision, "reason": "按整批留言判断", **kwargs},
            )
        ],
        usage=Usage(input_tokens=10, output_tokens=5),
    )


async def test_default_disabled_does_not_start_chat_or_idle(kernel, clock):
    receive(kernel)
    clock.advance(seconds=86400)
    assert (await kernel.scheduler.step())["status"] == "waiting"
    assert kernel.scheduler.next_at() is None
    assert kernel.activities.list() == []
    assert kernel.models.calls() == []


async def test_debounce_batches_once_and_terminal_decision_skips_remaining_tools(kernel, clock):
    reply = choice("reply", text="收到，明天再看")
    reply.tool_calls.append(note_call("这条工具不应该执行"))
    provider = ScriptedProvider(reply)
    enable(kernel, provider)
    first = receive(kernel, "要不要看电影")
    clock.advance(seconds=1)
    second = receive(kernel, "算了明天吧")
    assert (await kernel.scheduler.step())["status"] == "waiting"
    clock.advance(seconds=2)
    result = await kernel.scheduler.step()
    activity = kernel.activities.get(result["activity_id"])
    assert set(activity["trigger_event_ids"]) == {first, second}
    assert activity["status"] == "completed"
    assert {x["decision"] for x in kernel.inbox.list()} == {"reply"}
    assert len(kernel.outbox.list()) == 1
    assert kernel.db.rows("SELECT * FROM plugin_values") == []
    assert len(provider.requests) == 1
    for _ in range(3):
        clock.advance(seconds=30)
        assert (await kernel.scheduler.step())["status"] == "waiting"
    assert len(provider.requests) == 1
    assert len(kernel.outbox.list()) == 1


async def test_messages_arriving_during_model_call_belong_to_next_batch(kernel, clock):
    async def incoming():
        receive(kernel, "刚到的下一条")
        return choice()

    provider = ScriptedProvider(incoming, choice())
    enable(kernel, provider, debounce_seconds=0, conversation_cooldown_seconds=0)
    first = receive(kernel, "第一条")
    await kernel.scheduler.step()
    prompt = "\n".join(m.content for m in provider.requests[0].messages)
    assert "第一条" in prompt and "刚到的下一条" not in prompt
    assert next(x for x in kernel.inbox.list() if x["id"] == first)["decision"] == "no_reply"
    clock.advance(seconds=1)
    await kernel.scheduler.step()
    assert len(provider.requests) == 2
    records = json.loads(provider.requests[1].messages[-1].content.split("\n", 1)[1])
    assert [r["text"] for r in records if r["assigned"]] == ["刚到的下一条"]
    assert kernel.outbox.list() == []


async def test_sleep_is_raw_only_and_wake_reviews_separate_audiences(kernel, clock, monkeypatch):
    provider = ScriptedProvider(choice(), choice("reply", text="醒来看到啦"))
    enable(kernel, provider, debounce_seconds=0)
    wake_at = clock.now() + timedelta(hours=8)
    kernel.lifecycle.request_sleep(SleepRequest(earliest_wake_at=wake_at, latest_wake_at=wake_at))
    receive(kernel, "Alice 的私事")
    receive(kernel, "群里的普通闲聊", "group:friends", conversation_kind="group")
    with monkeypatch.context() as guard:
        guard.setattr(kernel.memory, "search", lambda **kwargs: pytest.fail("睡眠时检索了记忆"))
        for _ in range(3):
            await kernel.scheduler.step()
            clock.advance(seconds=3600)
    assert provider.requests == []
    assert kernel.activities.list() == []
    assert kernel.scheduler.next_at() == wake_at.isoformat(timespec="microseconds")
    clock.advance(seconds=18000)
    await kernel.scheduler.step()
    assert kernel.lifecycle.state()["phase"] == "waking"
    clock.advance(seconds=1)
    await kernel.scheduler.step()
    assert kernel.lifecycle.state()["phase"] == "awake"
    assert len(provider.requests) == 2
    assert all("Alice 的私事" not in m.content for m in provider.requests[1].messages)
    assert len(kernel.outbox.list()) == 1
    assert kernel.outbox.list()[0]["conversation_id"] == "group:friends"


async def test_old_messages_expire_without_a_model_call(kernel, clock):
    provider = ScriptedProvider()
    enable(kernel, provider, max_message_age_hours=1)
    receive(kernel)
    clock.advance(seconds=7200)
    await kernel.scheduler.step()
    assert kernel.inbox.list()[0]["decision"] == "expired"
    assert provider.requests == []


async def test_defer_requires_future_trigger_and_has_bounded_reconsideration(kernel, clock):
    revisit = (clock.now() + timedelta(minutes=1)).isoformat(timespec="microseconds")
    later = (clock.now() + timedelta(minutes=2)).isoformat(timespec="microseconds")
    provider = ScriptedProvider(
        choice("defer", revisit_at=revisit), choice("defer", revisit_at=later), answer("")
    )
    enable(kernel, provider, debounce_seconds=0, max_deferrals=1)
    receive(kernel)
    await kernel.scheduler.step()
    assert kernel.inbox.list()[0]["status"] == "deferred"
    assert kernel.scheduler.next_at() == revisit
    clock.advance(seconds=59)
    assert (await kernel.scheduler.step())["status"] == "waiting"
    clock.advance(seconds=1)
    await kernel.scheduler.step()
    assert kernel.inbox.list()[0]["decision"] == "no_reply"
    assert kernel.db.rows("SELECT review_count FROM inbox")[0]["review_count"] == 2
    assert "暂缓次数上限" in provider.requests[-1].messages[-1].content
    assert kernel.outbox.list() == []


async def test_background_yields_after_model_then_resumes_without_repeating(kernel, clock):
    async def interrupt():
        receive(kernel, "打断一下")
        return ModelReply(tool_calls=[note_call("先记住这张草稿")])

    provider = ScriptedProvider(interrupt, choice(), answer("草稿完成"))
    enable(kernel, provider)
    plan = kernel.activities.create(ActivityRequest(kind="plan", goal="画一张画"))
    result = await kernel.scheduler.step()
    assert result["result"]["yielded"] == "new_messages"
    assert kernel.db.rows("SELECT * FROM tool_calls") == []
    assert kernel.scheduler.next_at() == (clock.now() + timedelta(seconds=2)).isoformat(
        timespec="microseconds"
    )
    clock.advance(seconds=1)
    assert (await kernel.scheduler.step())["status"] == "waiting"
    assert len(provider.requests) == 1
    clock.advance(seconds=1)
    await kernel.scheduler.step()
    # 让出前台的等待不耗尽该活动的执行时限，也不重置已累计步骤。
    clock.advance(seconds=3600)
    resumed = await kernel.scheduler.step()
    assert resumed["activity_id"] == plan["id"]
    assert resumed["result"]["status"] == "completed"
    assert len(provider.requests) == 3
    assert len(kernel.db.rows("SELECT * FROM tool_calls WHERE tool='notebook__write'")) == 1
    assert kernel.activities.get(plan["id"])["steps"] == 4


async def test_manual_review_during_call_invalidates_reply_without_killing_queue(kernel, clock):
    event_id = receive(kernel, "已经解决了")

    async def manually_handled():
        kernel.inbox.resolve(
            ReviewDecision(event_ids=[event_id], decision="no_reply", reason="人工确认已解决")
        )
        return answer("不应该再发")

    provider = ScriptedProvider(manually_handled, choice())
    enable(kernel, provider, debounce_seconds=0)
    result = await kernel.scheduler.step()
    assert kernel.outbox.list() == []
    assert kernel.scheduler.jobs()[0]["status"] == "held"
    kernel.activities.transition(result["activity_id"], "cancelled")
    clock.advance(seconds=1)
    await kernel.scheduler.step()
    assert kernel.scheduler.jobs()[0]["status"] == "done"
    assert kernel.inbox.list()[0]["reason"] == "人工确认已解决"
    assert kernel.db.rows("SELECT * FROM inbox_leases") == []
    receive(kernel, "新的事", "private:bob")
    await kernel.scheduler.step()
    assert len(provider.requests) == 2


async def test_retry_waits_persisted_backoff_instead_of_spinning(kernel, clock):
    provider = ScriptedProvider(ProviderError("unavailable"), choice())
    enable(kernel, provider, debounce_seconds=0)
    receive(kernel)
    first = await kernel.scheduler.step()
    assert first["result"]["retry_at"]
    assert kernel.scheduler.jobs()[0]["status"] == "waiting"
    assert kernel.scheduler.next_at() == first["result"]["retry_at"]
    clock.advance(seconds=9)
    await kernel.scheduler.step()
    assert len(provider.requests) == 1
    clock.advance(seconds=1)
    await kernel.scheduler.step()
    assert len(provider.requests) == 2
    assert len(kernel.scheduler.jobs()) == 1


async def test_autonomous_limit_keeps_foreground_available(kernel, clock):
    provider = ScriptedProvider(answer("完成一次空闲活动"), choice())
    enable(kernel, provider, debounce_seconds=0, idle_interval_seconds=60)
    setting(kernel, "scheduler", autonomous_enabled=True)
    setting(kernel, "models", autonomous_daily_call_limit=1)
    clock.advance(seconds=3600)
    await kernel.scheduler.step()
    clock.advance(seconds=60)
    exhausted = await kernel.scheduler.step()
    assert exhausted["result"]["status"] == "paused"
    assert "autonomous-day:" in exhausted["result"]["reason"]
    assert len(provider.requests) == 1
    receive(kernel, "前台仍可以回应")
    clock.advance(seconds=1)
    await kernel.scheduler.step()
    assert len(provider.requests) == 2
    assert kernel.inbox.list()[0]["decision"] == "no_reply"


async def test_idle_wait_does_not_replay_missed_intervals(kernel, clock):
    until = (clock.now() + timedelta(days=2)).isoformat(timespec="microseconds")
    provider = ScriptedProvider(
        ModelReply(
            tool_calls=[
                ToolCall(
                    id="wait",
                    name="life__wait_until",
                    arguments={"until": until, "reason": "今天先休息"},
                )
            ]
        )
    )
    enable(kernel, provider)
    setting(kernel, "scheduler", autonomous_enabled=True)
    clock.advance(seconds=86400)
    await kernel.scheduler.step()
    assert len(provider.requests) == 1
    assert len(kernel.activities.list()) == 1
    assert kernel.scheduler.state()["next_idle_at"] == until
    assert kernel.scheduler.next_at() == until


async def test_sleep_decision_is_atomic_and_not_reapplied_after_restart(tmp_path, clock):
    path = tmp_path / "sleeping-character"
    kernel = Kernel(path, clock)
    wake = (clock.now() + timedelta(hours=8)).isoformat(timespec="microseconds")
    provider = ScriptedProvider(
        ModelReply(
            tool_calls=[
                ToolCall(
                    id="sleep",
                    name="life__sleep",
                    arguments={"earliest_wake_at": wake, "latest_wake_at": wake, "reason": "困了"},
                )
            ]
        )
    )
    enable(kernel, provider)
    plan = kernel.activities.create(ActivityRequest(kind="plan", goal="决定今天的作息"))
    await kernel.scheduler.step()
    assert kernel.lifecycle.state()["phase"] == "sleeping"
    assert kernel.db.rows("SELECT status FROM activity_decisions")[0]["status"] == "applied"
    kernel.close()
    recovered = Kernel(path, clock)
    try:
        assert recovered.lifecycle.state()["wake_at"] == wake
        recovered.lifecycle.wake()
        recovered.lifecycle.finish_waking()
        recovered.scheduler.finish(plan["id"])
        assert recovered.lifecycle.state()["phase"] == "awake"
        assert len(recovered.db.rows("SELECT * FROM audit WHERE event_type='sleep.requested'")) == 1
    finally:
        recovered.close()


async def test_disabled_lane_and_unknown_work_do_not_make_due_time_spin(kernel, clock):
    provider = ScriptedProvider(ProviderError("auth"))
    enable(kernel, provider, debounce_seconds=0)
    receive(kernel)
    result = await kernel.scheduler.step()
    assert kernel.scheduler.jobs()[0]["status"] == "held"
    assert kernel.scheduler.next_at() is None
    kernel.scheduler.resume(result["activity_id"])
    setting(kernel, "scheduler", chat_enabled=False)
    clock.advance(seconds=1)
    assert (await kernel.scheduler.step())["status"] == "waiting"
    assert kernel.scheduler.next_at() is None
    setting(kernel, "scheduler", chat_enabled=True)
    kernel.activities.claim(result["activity_id"])
    work = kernel.lifecycle.start_work("tool", result["activity_id"])
    kernel.lifecycle.finish_work(work, "unknown")
    assert kernel.scheduler.next_at() is None


async def test_continuous_chats_still_give_background_a_safe_step(kernel, clock):
    provider = ScriptedProvider(choice(), ModelReply(tool_calls=[note_call()]), choice())
    enable(kernel, provider, debounce_seconds=0, conversation_cooldown_seconds=0, max_chat_burst=1)
    plan = kernel.activities.create(ActivityRequest(kind="plan", goal="不要被持续聊天饿死"))
    receive(kernel)
    await kernel.scheduler.step()
    receive(kernel, "另一条")
    clock.advance(seconds=1)
    result = await kernel.scheduler.step()
    assert result["activity_id"] == plan["id"]
    assert result["result"]["yielded"] == "new_messages"
    assert kernel.activities.get(plan["id"])["steps"] == 1
    clock.advance(seconds=1)
    await kernel.scheduler.step()
    receive(kernel, "再来一条")
    clock.advance(seconds=1)
    await kernel.scheduler.step()
    assert kernel.activities.get(plan["id"])["steps"] == 2
    assert len(kernel.db.rows("SELECT * FROM tool_calls WHERE tool='notebook__write'")) == 1
    assert len(provider.requests) == 3


async def test_yielded_activity_continues_same_checkpoint_after_restart(tmp_path, clock):
    path = tmp_path / "yielded-character"
    kernel = Kernel(path, clock)

    async def incoming():
        receive(kernel)
        return ModelReply(tool_calls=[note_call()])

    enable(kernel, ScriptedProvider(incoming), debounce_seconds=0)
    plan = kernel.activities.create(ActivityRequest(kind="plan", goal="保留执行到一半的草稿"))
    await kernel.scheduler.step()
    assert kernel.activities.get(plan["id"])["steps"] == 1
    kernel.close()
    recovered = Kernel(path, clock)
    try:
        provider = ScriptedProvider(choice(), answer("完成"))
        recovered.models.register("primary", provider)
        clock.advance(seconds=1)
        await recovered.scheduler.step()
        clock.advance(seconds=1)
        await recovered.scheduler.step()
        assert len(provider.requests) == 2
        assert recovered.activities.get(plan["id"])["status"] == "completed"
        assert recovered.activities.get(plan["id"])["steps"] == 4
        assert len(recovered.db.rows("SELECT * FROM tool_calls WHERE tool='notebook__write'")) == 1
    finally:
        recovered.close()


async def test_failed_queue_does_not_start_new_roots_to_retry_forever(kernel, clock):
    provider = ScriptedProvider(ProviderError("auth"))
    enable(kernel, provider)
    setting(kernel, "scheduler", autonomous_enabled=True)
    clock.advance(seconds=3600)
    await kernel.scheduler.step()
    assert kernel.scheduler.jobs()[0]["status"] == "held"
    for _ in range(5):
        clock.advance(seconds=86400)
        assert (await kernel.scheduler.step())["status"] == "waiting"
        assert kernel.scheduler.next_at() is None
    assert len(provider.requests) == 1
    assert len(kernel.activities.list()) == 1


async def test_event_driven_runner_wakes_for_inbox_and_stops_cleanly(kernel, monkeypatch):
    provider = ScriptedProvider(choice())
    enable(kernel, provider, debounce_seconds=0)
    completed = asyncio.Event()
    original = kernel.scheduler.finish

    def finish(activity_id):
        original(activity_id)
        completed.set()

    monkeypatch.setattr(kernel.scheduler, "finish", finish)
    task = asyncio.create_task(kernel.run_timers())
    try:
        await asyncio.sleep(0)
        assert provider.requests == []
        receive(kernel, "通过真实运行循环接取")
        await asyncio.wait_for(completed.wait(), timeout=2)
        assert kernel.inbox.list()[0]["decision"] == "no_reply"
        assert len(provider.requests) == 1
        kernel.stop()
        await asyncio.wait_for(task, timeout=2)
        assert task.done() and not task.cancelled()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
