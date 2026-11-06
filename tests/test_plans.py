import json
import sqlite3
from datetime import timedelta
from uuid import uuid4

import pytest
from test_memory import activity, event, remember
from test_model_gateway import ScriptedProvider, answer
from test_scheduler import choice, enable, receive, setting

from mkbot.domain.errors import AdmissionDenied, Conflict, ContextInvalidated
from mkbot.domain.models import ActivityRequest, SleepRequest
from mkbot.models.contracts import ModelReply, ToolCall, Usage
from mkbot.runtime.kernel import Kernel
from mkbot.runtime.planning import ManualPlan, PlanEdit, PlanOutcome, PlanProposal


def progress(clock, decision="continue", **kwargs):
    args = {"decision": decision, "result": "整理了一张草图", "progress": "已完成构图"}
    if decision == "continue":
        args |= {
            "next_step": "下一次补充色彩",
            "due_at": (clock.now() + timedelta(seconds=60)).isoformat(),
        }
    return ModelReply(
        tool_calls=[ToolCall(id=str(uuid4()), name="plans__progress", arguments=args | kwargs)],
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def new_plan(kernel, clock, **kwargs):
    return kernel.plans.create(
        ManualPlan(goal="画一张四格漫画", next_step="先画构图", due_at=clock.now(), **kwargs)
    )


async def test_planner_creates_goal_once_and_retains_root_and_sources(kernel, clock):
    request = PlanProposal(
        goal="明天再聊这件私事",
        next_step="先确认对方愿不愿意聊",
        due_at=clock.now() + timedelta(seconds=60),
    )
    reply = ModelReply(
        tool_calls=[
            ToolCall(id="create", name="plans__create", arguments=request.model_dump(mode="json")),
            *choice().tool_calls,
        ],
        usage=Usage(input_tokens=10, output_tokens=5),
    )
    provider = ScriptedProvider(reply)
    enable(kernel, provider, debounce_seconds=0)
    source = receive(kernel, "我私下想和你聊的心事")
    result = await kernel.scheduler.step()
    plan = kernel.plans.list()[0]
    assert plan["conversation_id"] == "private:alice"
    assert plan["parent_id"] == plan["root_id"] == result["activity_id"]
    assert kernel.memory.source_references(plan["id"]) == ["event:" + source]
    assert plan["status"] == "waiting" and not plan["runs"]
    assert kernel.models.calls()[0]["activity_id"] == result["activity_id"]
    assert kernel.outbox.list() == []


async def test_progress_is_terminal_and_resume_after_restart_keeps_root(tmp_path, clock):
    directory = tmp_path / "plans"
    kernel = Kernel(directory, clock)
    first = progress(clock)
    first.tool_calls.append(
        ToolCall(id="extra", name="notebook__write", arguments={"key": "x", "value": {}})
    )
    provider = ScriptedProvider(first)
    enable(kernel, provider)
    plan = new_plan(kernel, clock)
    run1 = await kernel.scheduler.step()
    saved = kernel.plans.get(plan["id"])
    assert saved["progress"] == "已完成构图"
    assert saved["due_at"] is not None and len(saved["runs"]) == 1
    assert kernel.db.rows("SELECT * FROM plugin_values") == []
    steps = kernel.activities.get(plan["id"])["steps"]
    assert steps == 3
    assert (await kernel.scheduler.step())["status"] == "waiting"
    kernel.close()
    kernel = Kernel(directory, clock)
    try:
        second = ScriptedProvider(progress(clock, "completed", progress="漫画已完成"))
        enable(kernel, second)
        clock.advance(seconds=60)
        run2 = await kernel.scheduler.step()
        saved = kernel.plans.get(plan["id"])
        assert saved["status"] == "completed" and saved["progress"] == "漫画已完成"
        assert saved["steps"] == steps + 3
        for run in (run1, run2):
            assert kernel.activities.get(run["activity_id"])["root_id"] == plan["id"]
        assert len(kernel.models.calls()) == 2 and not kernel.outbox.list()
        kernel.scheduler.finish(run2["activity_id"])
        assert kernel.plans.get(plan["id"])["version"] == saved["version"]
        clock.advance(seconds=86400)
        assert (await kernel.scheduler.step())["status"] == "waiting"
    finally:
        kernel.close()


async def test_future_goal_does_not_block_sleep_and_sleep_starts_no_plan(kernel, clock):
    provider = ScriptedProvider(progress(clock, "completed"))
    enable(kernel, provider)
    new_plan(kernel, clock)
    wake = clock.now() + timedelta(hours=1)
    kernel.lifecycle.request_sleep(SleepRequest(earliest_wake_at=wake, latest_wake_at=wake))
    assert kernel.lifecycle.state()["phase"] == "sleeping"
    clock.advance(seconds=600)
    await kernel.scheduler.step()
    assert not provider.requests and not kernel.db.rows("SELECT * FROM plan_runs")
    clock.advance(seconds=3000)
    await kernel.scheduler.step()
    assert len(provider.requests) == 1


async def test_repeated_scheduling_cannot_reset_cumulative_execution_budget(kernel, clock):
    provider = ScriptedProvider(progress(clock), progress(clock, "completed"))
    enable(kernel, provider)
    setting(kernel, "execution", max_steps=3)
    plan = new_plan(kernel, clock)
    await kernel.scheduler.step()
    clock.advance(seconds=60)
    result = await kernel.scheduler.step()
    assert result["result"]["status"] == "paused"
    assert len(provider.requests) == 1
    assert kernel.plans.get(plan["id"])["steps"] == 3
    assert kernel.scheduler.next_at() is None
    for _ in range(3):
        clock.advance(seconds=3600)
        await kernel.scheduler.step()
    assert len(kernel.plans.get(plan["id"])["runs"]) == 2
    assert len(provider.requests) == 1


def test_private_goal_context_and_forgetting_propagate_to_goal_and_children(kernel, clock):
    source = event(kernel, clock, text="我的秘密")
    memory = remember(kernel, source, text="我的秘密", tier="long")
    parent = activity(kernel)
    kernel.memory.select_refs(parent, [{"id": memory["id"], "revision": memory["revision"]}])
    kernel.memory.select_sources(parent, ["event:" + source])
    plan = kernel.plans.create(
        PlanProposal(
            goal="帮我保守我的秘密",
            next_step="私下问候",
            due_at=clock.now() + timedelta(seconds=60),
        ),
        activity_id=parent,
        key="plan1",
    )
    bob = activity(kernel, "private:bob")
    assert kernel.plans.context(bob) == []
    owner = activity(kernel)
    assert kernel.plans.context(owner)[0]["id"] == plan["id"]
    enable(kernel, ScriptedProvider())
    clock.advance(seconds=60)
    kernel.plans.dispatch_due()
    child = kernel.plans.get(plan["id"])["runs"][0]["id"]
    kernel.memory.forget(memory["id"], memory["revision"])
    saved = kernel.plans.get(plan["id"])
    assert saved["status"] == "paused" and saved["due_at"] is None
    assert "秘密" not in json.dumps(saved, ensure_ascii=False)
    for key in (plan["id"], child, owner):
        with pytest.raises(ContextInvalidated):
            kernel.assert_context(key)
    assert "秘密" not in kernel.activities.get(child)["goal"]


async def test_goal_yields_to_chat_without_forking_new_execution(kernel, clock):
    async def interrupted():
        receive(kernel, "有人来了")
        return progress(clock, "completed")

    provider = ScriptedProvider(interrupted, choice())
    enable(kernel, provider, debounce_seconds=0)
    plan = new_plan(kernel, clock)
    first = await kernel.scheduler.step()
    assert first["result"]["yielded"] == "new_messages"
    clock.advance(seconds=1)
    await kernel.scheduler.step()
    clock.advance(seconds=1)
    resumed = await kernel.scheduler.step()
    assert resumed["activity_id"] == first["activity_id"]
    assert kernel.plans.get(plan["id"])["status"] == "completed"
    assert len(kernel.plans.get(plan["id"])["runs"]) == 1
    assert len(provider.requests) == 2


async def test_missing_progress_stops_and_run_cap_prevents_empty_repeats(kernel, clock):
    async def continuing():
        return progress(clock)

    provider = ScriptedProvider(answer(""), continuing)
    enable(kernel, provider, max_runs_per_plan=1)
    no_decision = new_plan(kernel, clock)
    await kernel.scheduler.step()
    assert kernel.plans.get(no_decision["id"])["status"] == "paused"
    second = new_plan(kernel, clock)
    clock.advance(seconds=1)
    await kernel.scheduler.step()
    clock.advance(seconds=60)
    await kernel.scheduler.step()
    saved = kernel.plans.get(second["id"])
    assert saved["status"] == "paused" and "上限" in saved["last_result"]
    assert len(provider.requests) == 2 and len(saved["runs"]) == 1
    assert kernel.scheduler.next_at() is None


def test_creation_caps_survive_cancellation_and_operator_edits_use_version(kernel, clock):
    setting(kernel, "scheduler", max_plans_per_root=1)
    parent = activity(kernel)
    request = PlanProposal(goal="陪朋友画画", next_step="先问问画什么")
    plan = kernel.plans.create(request, activity_id=parent, key="first")
    duplicate = kernel.plans.create(request, activity_id=parent, key="first")
    assert plan["id"] == duplicate["id"]
    kernel.plans.edit(
        plan["id"],
        PlanEdit(**request.model_dump(), expected_version=plan["version"], status="cancelled"),
    )
    with pytest.raises(AdmissionDenied):
        kernel.plans.create(request, activity_id=parent, key="second")
    with pytest.raises(Conflict):
        kernel.plans.edit(
            plan["id"], PlanEdit(**request.model_dump(), expected_version=plan["version"])
        )
    with pytest.raises(Conflict):
        kernel.activities.claim(plan["id"])


async def test_held_execution_cannot_be_bypassed_by_editing_goal(kernel, clock):
    provider = ScriptedProvider(progress(clock))
    enable(kernel, provider)
    plan = new_plan(kernel, clock)
    kernel.plans.dispatch_due()
    saved = kernel.plans.get(plan["id"])
    with pytest.raises(Conflict, match="未收尾"):
        kernel.plans.edit(
            plan["id"],
            PlanEdit(
                goal="改名",
                next_step="重新开始",
                expected_version=saved["version"],
                due_at=clock.now(),
            ),
        )
    child = saved["runs"][0]["id"]
    kernel.activities.transition(child, "cancelled")
    kernel.plans.reconcile()
    assert kernel.plans.get(plan["id"])["status"] == "paused"
    assert kernel.plans.next_at() is None


def test_v5_migration_preserves_existing_activity(tmp_path, clock):
    directory = tmp_path / "migration"
    old = Kernel(directory, clock)
    existing = old.activities.create(ActivityRequest(kind="plan", goal="迁移前已有的计划"))
    old.close()
    with sqlite3.connect(directory / "mkbot.sqlite3") as tx:
        tx.executescript("DROP TABLE plan_runs; DROP TABLE activity_plans; PRAGMA user_version=5;")
    new = Kernel(directory, clock)
    try:
        assert new.activities.get(existing["id"])["goal"] == "迁移前已有的计划"
        assert new.db.rows("PRAGMA user_version")[0]["user_version"] == 6
        assert new.plans.list() == []
    finally:
        new.close()


async def test_unchanged_progress_and_next_step_are_not_rescheduled(kernel, clock):
    async def same_result():
        return progress(clock)

    provider = ScriptedProvider(same_result, same_result)
    enable(kernel, provider)
    plan = new_plan(kernel, clock)
    await kernel.scheduler.step()
    clock.advance(seconds=60)
    await kernel.scheduler.step()
    saved = kernel.plans.get(plan["id"])
    assert saved["status"] == "paused" and saved["due_at"] is None
    assert "未变化" in saved["last_result"]
    assert kernel.scheduler.next_at() is None


async def test_expired_evidence_holds_due_goal_without_model_call_or_timer_spin(kernel, clock):
    from mkbot.memory.contracts import ManualMemory

    memory = kernel.memory.create_manual(
        ManualMemory(
            kind="current_state",
            subjects=["self"],
            content="这次的临时计划",
            scope=["@self"],
            valid_until=clock.now() + timedelta(seconds=30),
        )
    )
    parent = activity(kernel, None)
    kernel.memory.select_refs(parent, [{"id": memory["id"], "revision": 1}])
    plan = kernel.plans.create(
        PlanProposal(
            goal="根据临时安排画画",
            next_step="先看看天气",
            due_at=clock.now() + timedelta(seconds=60),
        ),
        activity_id=parent,
        key="expiry",
    )
    kernel.activities.transition(parent, "completed")
    provider = ScriptedProvider()
    enable(kernel, provider)
    clock.advance(seconds=60)
    await kernel.scheduler.step()
    assert not provider.requests
    assert kernel.plans.get(plan["id"])["status"] == "paused"
    assert kernel.scheduler.next_at() is None


async def test_unknown_work_on_same_root_blocks_new_plan_execution(kernel, clock):
    parent = activity(kernel, None)
    plan = kernel.plans.create(
        PlanProposal(
            goal="等待上一步后继续画画",
            next_step="补上细节",
            due_at=clock.now() + timedelta(seconds=60),
        ),
        activity_id=parent,
        key="unknown",
    )
    work = kernel.lifecycle.start_work("tool", parent)
    kernel.lifecycle.finish_work(work, "unknown")
    provider = ScriptedProvider()
    enable(kernel, provider)
    clock.advance(seconds=60)
    await kernel.scheduler.step()
    saved = kernel.plans.get(plan["id"])
    assert saved["status"] == "paused" and saved["due_at"] is None
    assert "未知结果" in saved["last_result"]
    assert not saved["runs"] and not provider.requests
    assert kernel.scheduler.next_at() is None


async def test_operator_can_cancel_paused_goal_without_inventing_next_step(kernel, clock):
    provider = ScriptedProvider(progress(clock, "pause", next_step=""))
    enable(kernel, provider)
    plan = new_plan(kernel, clock)
    await kernel.scheduler.step()
    saved = kernel.plans.get(plan["id"])
    cancelled = kernel.plans.edit(
        plan["id"],
        PlanEdit(
            goal=saved["goal"], next_step="", expected_version=saved["version"], status="cancelled"
        ),
    )
    assert cancelled["status"] == "cancelled"


async def test_completed_run_with_expired_context_is_reconciled_without_stopping_loop(
    kernel, clock
):
    from mkbot.memory.contracts import ManualMemory

    plan = new_plan(kernel, clock)
    evidence = kernel.memory.create_manual(
        ManualMemory(
            kind="current_state",
            subjects=["self"],
            content="短暂有效的观察",
            scope=["@self"],
            valid_until=clock.now() + timedelta(seconds=30),
        )
    )
    enable(kernel, ScriptedProvider())
    kernel.scheduler._adopt_plans()
    child = kernel.plans.get(plan["id"])["runs"][0]["id"]
    kernel.activities.claim(child)
    kernel.memory.select_refs(child, [{"id": evidence["id"], "revision": 1}])
    kernel.plans.choose(
        child, PlanOutcome(decision="completed", result="临时观察的结果", progress="本轮的完成记录")
    )
    kernel.activities.transition(child, "completed")
    # 模拟已结束活动、尚未提交计划收尾时进程中断，并在资料过期后恢复。
    clock.advance(seconds=60)
    await kernel.scheduler.step()
    saved = kernel.plans.get(plan["id"])
    assert saved["status"] == "paused" and saved["due_at"] is None
    assert saved["progress"] == ""
    decision = kernel.db.rows(
        "SELECT status FROM activity_decisions WHERE activity_id=?", (child,)
    )[0]
    assert decision["status"] == "invalidated"
    assert (await kernel.scheduler.step())["status"] == "waiting"
