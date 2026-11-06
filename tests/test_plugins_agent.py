import asyncio

import pytest
from pydantic import Field
from test_model_gateway import MESSAGES, ScriptedProvider, answer, configured

from mkbot.domain.errors import BudgetExceeded, Conflict
from mkbot.domain.models import ActivityRequest, InboundEvent, StrictModel
from mkbot.models.contracts import ModelReply, ToolCall
from mkbot.plugins.contracts import Manifest, Plugin, Tool, ToolOutcomeUnknown, ToolResult


def note_call(text="创作草稿", call_id="call-1"):
    return ToolCall(id=call_id, name="notebook__write", arguments={"key": "draft", "text": text})


async def test_tool_receipt_deduplication_does_not_write_twice(kernel):
    activity = configured(kernel, ScriptedProvider())
    first = await kernel.plugins.execute(activity, "tool-1", note_call())
    second = await kernel.plugins.execute(activity, "tool-1", note_call())
    assert first == second
    assert len(kernel.db.rows("SELECT * FROM tool_calls")) == 1
    with pytest.raises(Conflict):
        await kernel.plugins.execute(activity, "tool-1", note_call("改变参数"))


async def test_plugin_storage_is_scoped_to_conversation(kernel):
    activity = configured(kernel, ScriptedProvider())
    await kernel.plugins.execute(activity, "tool-1", note_call())
    other = kernel.activities.create(
        ActivityRequest(kind="chat", goal="群聊任务", conversation_id="group:one")
    )["id"]
    kernel.activities.claim(other)
    result = await kernel.plugins.execute(
        other, "tool-2", ToolCall(id="two", name="notebook__read", arguments={"key": "draft"})
    )
    assert result.data["note"] is None


async def test_unknown_external_effect_is_not_repeated(kernel):
    calls = []

    class Args(StrictModel):
        text: str = Field(min_length=1)

    async def external(context, arguments):
        calls.append(arguments.text)
        raise TimeoutError

    plugin = Plugin(
        Manifest(id="example", name="测试", version="1", description="外部副作用测试"),
        (Tool("write", "写到模拟外部服务", Args, external, effect="external"),),
    )
    kernel.plugins.register(plugin)
    activity = configured(kernel, ScriptedProvider())
    call = ToolCall(id="one", name="example__write", arguments={"text": "一次"})
    for _ in range(2):
        with pytest.raises(ToolOutcomeUnknown):
            await kernel.plugins.execute(activity, "one", call)
    assert calls == ["一次"]
    assert kernel.lifecycle.state()["work"][0]["status"] == "unknown"


async def test_plugin_model_calls_inherit_root_budget(kernel):
    class Args(StrictModel):
        pass

    async def expert(context, arguments):
        await context.model("first", MESSAGES)
        with pytest.raises(BudgetExceeded):
            await context.model("second", MESSAGES)
        return ToolResult()

    kernel.plugins.register(
        Plugin(
            Manifest(id="expert", name="专家", version="1", description="测试模型服务"),
            (Tool("think", "调用公共模型服务", Args, expert),),
        )
    )
    provider = ScriptedProvider(answer())
    activity = configured(kernel, provider, root_call_limit=1)
    await kernel.plugins.execute(
        activity, "tool-1", ToolCall(id="one", name="expert__think", arguments={})
    )
    assert len(provider.requests) == 1
    assert kernel.models.calls()[0]["activity_id"] == activity


async def test_agent_uses_one_context_and_ends_with_one_queued_reply(kernel, clock):
    provider = ScriptedProvider(ModelReply(tool_calls=[note_call()]), answer("草稿已记下"))
    activity = configured(kernel, provider)
    with kernel.db.transaction() as tx:
        tx.execute("UPDATE activities SET conversation_id='private:alice' WHERE id=?", (activity,))
    for conversation, text in (("private:alice", "我想画漫画"), ("private:bob", "B 的私人心事")):
        kernel.inbox.receive(
            InboundEvent(
                source="test",
                external_id=conversation,
                sender_id="friend",
                conversation_id=conversation,
                text=text,
                occurred_at=clock.now(),
            )
        )
    result = await kernel.agent.run(activity)
    assert result["status"] == "completed"
    assert len(provider.requests) == 2
    assert provider.requests[0].tools
    assert any("随手记" in m.content for m in provider.requests[0].messages)
    assert any("我想画漫画" in m.content for m in provider.requests[0].messages)
    assert all("B 的私人心事" not in m.content for r in provider.requests for m in r.messages)
    assert provider.requests[1].messages[-1].role == "tool"
    assert len(kernel.outbox.list()) == 1
    assert kernel.outbox.list()[0]["payload"]["text"] == "草稿已记下"
    await kernel.agent.run(activity)
    assert len(provider.requests) == 2
    assert len(kernel.outbox.list()) == 1


async def test_no_progress_loop_pauses_instead_of_spending_forever(kernel):
    provider = ScriptedProvider(
        *(ModelReply(tool_calls=[note_call(call_id=f"call-{i}")]) for i in range(10))
    )
    activity = configured(kernel, provider)
    result = await kernel.agent.run(activity)
    assert result["status"] == "paused"
    assert "重复执行" in result["reason"]
    assert len(provider.requests) == 2
    assert kernel.outbox.list() == []
    assert kernel.activities.get(activity)["steps"] == 4


async def test_step_limit_stops_even_when_tool_arguments_keep_changing(kernel):
    config = kernel.config.get("execution")
    kernel.config.put("execution", config["content"] | {"max_steps": 3}, config["revision"])
    provider = ScriptedProvider(
        *(ModelReply(tool_calls=[note_call(text=str(i), call_id=f"call-{i}")]) for i in range(10))
    )
    activity = configured(kernel, provider)
    result = await kernel.agent.run(activity)
    assert result["status"] == "paused"
    assert "步骤" in result["reason"]
    assert len(provider.requests) == 2
    assert len(kernel.db.rows("SELECT * FROM tool_calls")) == 1
    assert (await kernel.agent.run(activity))["status"] == "paused"
    assert len(provider.requests) == 2


async def test_model_choice_can_end_without_reply(kernel):
    activity = configured(kernel, ScriptedProvider(ModelReply(text="")))
    assert (await kernel.agent.run(activity))["status"] == "completed"
    assert kernel.outbox.list() == []


async def test_runtime_pause_during_model_response_preserves_pending_decision(kernel):
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed():
        entered.set()
        await release.wait()
        return answer("醒来后再说")

    provider = ScriptedProvider(delayed)
    activity = configured(kernel, provider)
    task = asyncio.create_task(kernel.agent.run(activity))
    await entered.wait()
    kernel.lifecycle.pause(True)
    release.set()
    assert (await task)["status"] == "paused"
    assert kernel.outbox.list() == []
    kernel.lifecycle.pause(False)
    assert (await kernel.agent.run(activity))["status"] == "completed"
    assert len(provider.requests) == 1
