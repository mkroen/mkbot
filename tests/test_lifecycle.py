import asyncio
from datetime import timedelta

import pytest

from mkbot.domain.errors import AdmissionDenied, Conflict
from mkbot.domain.models import ActivityRequest, InboundEvent, SleepRequest
from mkbot.runtime.kernel import Kernel


def sleep_request(clock):
    return SleepRequest(
        earliest_wake_at=clock.now() + timedelta(hours=6),
        latest_wake_at=clock.now() + timedelta(hours=7),
        reason="按生物钟休息",
    )


def test_sleep_persists_incoming_without_model_or_delivery(kernel, clock):
    state = kernel.lifecycle.request_sleep(sleep_request(clock), fraction=0.5)
    assert state["phase"] == "sleeping"
    kernel.inbox.receive(
        InboundEvent(
            source="test",
            external_id="one",
            conversation_id="private:a",
            sender_id="a",
            directed=True,
            text="醒来再看",
            occurred_at=clock.now(),
        )
    )
    for kind in ("model", "tool", "embedding", "reflection", "delivery"):
        with pytest.raises(AdmissionDenied):
            kernel.lifecycle.start_work(kind)
    with pytest.raises(AdmissionDenied):
        kernel.outbox.enqueue("reply-one", "private:a", {"text": "不能发"})
    assert len(kernel.inbox.batches()) == 1
    assert kernel.lifecycle.state()["phase"] == "sleeping"
    assert kernel.db.rows("SELECT * FROM work") == []
    assert kernel.outbox.list() == []


async def test_inflight_work_finishes_before_sleep(kernel, clock):
    started, release = asyncio.Event(), asyncio.Event()

    async def generation():
        async with kernel.lifecycle.work("model"):
            started.set()
            await release.wait()

    task = asyncio.create_task(generation())
    await started.wait()
    state = kernel.lifecycle.request_sleep(sleep_request(clock))
    assert state["phase"] == "winding_down"
    with pytest.raises(AdmissionDenied):
        kernel.lifecycle.start_work("model")
    release.set()
    await task
    assert kernel.lifecycle.state()["phase"] == "sleeping"


def test_running_activity_can_finish_its_tools_but_no_new_activity(kernel, clock):
    activity = kernel.activities.create(ActivityRequest(kind="comic", goal="完成漫画"))
    kernel.activities.claim(activity["id"])
    kernel.lifecycle.request_sleep(sleep_request(clock))
    work_id = kernel.lifecycle.start_work("tool", activity["id"])
    with pytest.raises(AdmissionDenied):
        kernel.activities.transition(activity["id"], "paused")
    kernel.lifecycle.finish_work(work_id, "succeeded")
    assert kernel.lifecycle.state()["phase"] == "winding_down"
    kernel.activities.transition(activity["id"], "completed")
    assert kernel.lifecycle.state()["phase"] == "sleeping"


def test_future_goal_does_not_prevent_sleep(kernel, clock):
    activity = kernel.activities.create(
        ActivityRequest(
            kind="read",
            goal="明天继续阅读",
            due_at=clock.now() + timedelta(days=1),
        )
    )
    with pytest.raises(AdmissionDenied):
        kernel.activities.claim(activity["id"])
    assert kernel.lifecycle.request_sleep(sleep_request(clock))["phase"] == "sleeping"


def test_restart_keeps_selected_time_and_wakes_once(tmp_path, clock):
    first = Kernel(tmp_path, clock)
    selected = first.lifecycle.request_sleep(sleep_request(clock), fraction=0.25)["wake_at"]
    first.close()
    second = Kernel(tmp_path, clock)
    assert second.lifecycle.state()["wake_at"] == selected
    assert second.lifecycle.state()["phase"] == "sleeping"
    second.close()
    clock.advance(seconds=8 * 3600)
    third = Kernel(tmp_path, clock)
    try:
        assert third.lifecycle.state()["phase"] == "waking"
        third.lifecycle.tick()
        assert len([e for e in third.audit() if e["event_type"] == "sleep.waking"]) == 1
        third.lifecycle.finish_waking()
        assert third.lifecycle.state()["phase"] == "awake"
    finally:
        third.close()


def test_pause_does_not_erase_sleep_or_wake_early(kernel, clock):
    kernel.lifecycle.request_sleep(sleep_request(clock))
    kernel.lifecycle.pause(True)
    clock.advance(seconds=9 * 3600)
    assert not kernel.lifecycle.tick()
    assert kernel.lifecycle.state()["phase"] == "sleeping"
    with pytest.raises(AdmissionDenied):
        kernel.lifecycle.wake()
    assert kernel.lifecycle.pause(False)["phase"] == "waking"


async def test_uncertain_tool_outcome_blocks_sleep(kernel, clock):
    with pytest.raises(TimeoutError):
        async with kernel.lifecycle.work("tool"):
            kernel.lifecycle.request_sleep(sleep_request(clock))
            raise TimeoutError()
    state = kernel.lifecycle.state()
    assert state["phase"] == "winding_down"
    assert state["work"][0]["status"] == "unknown"
    kernel.lifecycle.finish_work(state["work"][0]["id"], "failed", detail="已核对远端失败")
    assert kernel.lifecycle.state()["phase"] == "sleeping"


def test_restart_reconciles_orphan_and_keeps_checkpoint(tmp_path, clock):
    first = Kernel(tmp_path, clock)
    activity = first.activities.create(ActivityRequest(kind="comic", goal="保留创作进度"))
    claimed = first.activities.claim(activity["id"])
    first.activities.checkpoint(activity["id"], claimed["version"], {"project_id": "comic-1"})
    work_id = first.lifecycle.start_work("tool", activity["id"])
    first.close()
    second = Kernel(tmp_path, clock)
    try:
        assert second.activities.get(activity["id"])["status"] == "interrupted"
        assert second.activities.get(activity["id"])["checkpoint"] == {"project_id": "comic-1"}
        with pytest.raises(AdmissionDenied):
            second.activities.claim(activity["id"])
        second.lifecycle.finish_work(work_id, "succeeded", detail="通过作业 ID 已核对完成")
        second.activities.claim(activity["id"])
    finally:
        second.close()


def test_instance_directory_has_one_live_owner(tmp_path, clock):
    first = Kernel(tmp_path, clock)
    try:
        with pytest.raises(Conflict):
            Kernel(tmp_path, clock)
    finally:
        first.close()
    second = Kernel(tmp_path, clock)
    second.close()


async def test_timer_shutdown_does_not_wait_for_wake(kernel, clock):
    kernel.lifecycle.request_sleep(sleep_request(clock))
    task = asyncio.create_task(kernel.run_timers())
    await asyncio.sleep(0)
    kernel.stop()
    await asyncio.wait_for(task, timeout=0.5)
