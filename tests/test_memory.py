import asyncio
import json
from datetime import timedelta

import pytest
from test_model_gateway import ScriptedProvider, answer, configured

from mkbot.domain.errors import AdmissionDenied, Conflict, ContextInvalidated, NotFound
from mkbot.domain.models import ActivityRequest, InboundEvent, SleepRequest
from mkbot.memory.contracts import MemoryEdit, MemoryProposal
from mkbot.models.contracts import ModelReply, ToolCall
from mkbot.runtime.kernel import Kernel


def event(
    kernel,
    clock,
    *,
    text="我喜欢安静",
    conversation="private:alice",
    sender="alice",
    kind="private",
):
    return kernel.inbox.receive(
        InboundEvent(
            source="qq",
            external_id=f"event-{len(kernel.inbox.list())}",
            conversation_id=conversation,
            sender_id=sender,
            conversation_kind=kind,
            text=text,
            occurred_at=clock.now(),
        )
    )["id"]


def remember(kernel, source, *, text="喜欢安静", subjects=None, **kwargs):
    return kernel.memory.propose(
        MemoryProposal(
            kind=kwargs.pop("kind", "preference"),
            subjects=subjects or ["qq/alice"],
            content=text,
            source_refs=["event:" + source],
            **kwargs,
        ),
        operator=True,
    )


def activity(kernel, conversation="private:alice", triggers=None):
    row = kernel.activities.create(
        ActivityRequest(
            kind="chat",
            goal="理解对方的近况",
            conversation_id=conversation,
            trigger_event_ids=triggers or [],
        )
    )
    return kernel.activities.claim(row["id"])["id"]


def complete_and_deliver(kernel, activity_id):
    row = kernel.activities.get(activity_id)
    kernel.activities.checkpoint(
        activity_id, row["version"], {"agent": {"result": {"text": "已回应"}}}
    )
    delivery = kernel.outbox.enqueue(
        "reply:" + activity_id, row["conversation_id"], {"text": "已回应"}, activity_id=activity_id
    )
    kernel.activities.transition(activity_id, "completed")
    kernel.outbox.claim(delivery["id"])
    kernel.outbox.settle(delivery["id"], "sent", receipt={"message_id": activity_id})


def test_private_derivations_keep_boundary_and_group_memories_can_be_shared(kernel, clock):
    private = remember(kernel, event(kernel, clock), tier="long")
    derived = kernel.memory.propose(
        MemoryProposal(
            kind="experience",
            subjects=["self"],
            content="和 Alice 聊过她的偏好",
            tier="long",
            source_refs=["memory:" + private["id"]],
        ),
        operator=True,
    )
    assert derived["source_refs"] == private["source_refs"]
    assert kernel.memory.search(audience="group:friends") == []
    assert kernel.memory.search(audience="private:bob") == []
    assert len(kernel.memory.search(audience="private:alice")) == 1
    public = remember(
        kernel,
        event(kernel, clock, conversation="group:friends", kind="group", text="喜欢星空"),
        text="喜欢星空",
    )
    assert kernel.memory.search(audience="private:bob")[0]["id"] == public["id"]
    with pytest.raises(Conflict):
        kernel.memory.propose(
            MemoryProposal(
                kind="experience",
                subjects=["self"],
                content="公开私聊摘要",
                source_refs=["memory:" + private["id"]],
                use_scope=["*"],
                disclosure_scope=["*"],
            ),
            operator=True,
        )


def test_operator_can_explicitly_share_one_fact_without_sharing_other_private_facts(kernel, clock):
    first = remember(kernel, event(kernel, clock), tier="long")
    second = remember(
        kernel, event(kernel, clock, text="另一件私事"), text="另一件私事", tier="long"
    )
    with pytest.raises(Conflict):
        kernel.memory.revise(
            first["id"], MemoryEdit(expected_revision=1, use_scope=["*"], disclosure_scope=["*"])
        )
    kernel.memory.revise(
        first["id"],
        MemoryEdit(
            expected_revision=1, use_scope=["*"], disclosure_scope=["*"], allow_wider_sharing=True
        ),
    )
    assert {row["id"] for row in kernel.memory.search(audience="group:new")} == {first["id"]}
    assert kernel.memory.get(second["id"])["revision"] == 1


def test_fifo_limits_one_busy_group_without_evicting_working_checkpoint(kernel, clock):
    settings = kernel.config.get("memory")
    kernel.config.put(
        "memory",
        settings["content"] | {"recent_capacity": 4, "recent_per_scope": 2},
        settings["revision"],
    )
    other = remember(kernel, event(kernel, clock), text="私聊未完的话题")
    task = activity(kernel)
    row = kernel.activities.get(task)
    kernel.activities.checkpoint(
        task, row["version"], {"comic": {"page": 3, "artifact_id": "image-3"}}
    )
    ids = []
    for i in range(8):
        ids.append(
            remember(
                kernel,
                event(kernel, clock, text=str(i), conversation="group:busy", kind="group"),
                text=f"群话题 {i}",
            )["id"]
        )
        clock.advance(seconds=1)
    active = [row for row in kernel.memory.list() if row["tier"] == "recent"]
    assert {row["id"] for row in active} == {other["id"], *ids[-2:]}
    assert kernel.activities.get(task)["checkpoint"]["comic"]["page"] == 3
    assert len(kernel.inbox.list()) == 9


def test_reads_do_not_reinforce_and_real_use_is_bounded_and_idempotent(kernel, clock):
    source = event(kernel, clock)
    memory = remember(kernel, source, valid_until=clock.now() + timedelta(days=5))
    for _ in range(100):
        kernel.memory.get(memory["id"])
        kernel.memory.search(audience="private:alice")
    assert kernel.memory.get(memory["id"])["activation"] == 0
    original = kernel.memory.get(memory["id"])
    for index in range(3):
        trigger = event(kernel, clock, text=f"新互动 {index}")
        task = activity(kernel, triggers=[trigger])
        kernel.memory.search(audience="private:alice", activity_id=task, select=True)
        kernel.memory.claim_use(task, [memory["id"]], "根据偏好选择安静的活动")
        assert kernel.memory.commit_uses(task) == 0
        complete_and_deliver(kernel, task)
        assert kernel.memory.commit_uses(task) == 0
    result = kernel.memory.get(memory["id"])
    assert result["activation"] == 2
    assert result["bucket"] == "protected"
    for key in (
        "confidence",
        "occurred_at",
        "valid_until",
        "source_refs",
        "revision",
        "importance",
        "disclosure_scope",
    ):
        assert result[key] == original[key]


def test_same_external_trigger_cannot_reward_new_roots_repeatedly(kernel, clock):
    memory = remember(kernel, event(kernel, clock))
    trigger = event(kernel, clock, text="同一次真实互动")
    for _ in range(3):
        task = activity(kernel, triggers=[trigger])
        kernel.memory.search(audience="private:alice", activity_id=task, select=True)
        kernel.memory.claim_use(task, [memory["id"]], "使用一次")
        complete_and_deliver(kernel, task)
    assert kernel.memory.get(memory["id"])["activation"] == 1


def test_batch_inputs_and_zero_reward_cannot_be_replayed_next_day(kernel, clock):
    source = event(kernel, clock)
    memory = remember(kernel, source, tier="long")
    config = kernel.config.get("memory")
    kernel.config.put("memory", config["content"] | {"daily_reward_limit": 1}, config["revision"])
    triggers = [event(kernel, clock, text=f"新的问题 {i}") for i in range(3)]

    def use(inputs):
        task = activity(kernel, triggers=inputs)
        kernel.memory.search(audience="private:alice", activity_id=task, select=True)
        kernel.memory.claim_use(task, [memory["id"]], "照顾对方偏好")
        complete_and_deliver(kernel, task)

    use([source])
    assert kernel.memory.get(memory["id"])["activation"] == 0
    use(triggers[:2])
    use(triggers[2:])
    before = kernel.memory.get(memory["id"])
    assert before["activation"] == 1
    clock.advance(seconds=25 * 3600)
    use(triggers)
    after = kernel.memory.get(memory["id"])
    assert after["activation"] == 1
    assert after["last_used_at"] == before["last_used_at"]
    assert len(kernel.db.rows("SELECT * FROM memory_uses")) == 2


def test_model_derivation_does_not_become_new_evidence_or_self_report(kernel, clock):
    item = remember(kernel, event(kernel, clock), kind="inference", text="可能喜欢安静")
    derived = kernel.memory.propose(
        MemoryProposal(
            kind="fact",
            subjects=["qq/alice"],
            content="喜欢安静",
            tier="long",
            source_refs=["memory:" + item["id"]],
        ),
        operator=True,
    )
    assert derived["confidence"] == "inferred"
    assert derived["source_refs"] == item["source_refs"]
    assert len(kernel.memory.search(audience="private:alice")) == 1
    own = event(kernel, clock, sender="mkbot", text="我反复说这是真的")
    with pytest.raises(NotFound):
        remember(kernel, own)


def test_new_self_report_replaces_hot_old_fact_and_third_party_cannot(kernel, clock):
    old = remember(kernel, event(kernel, clock), tier="long")
    with kernel.db.transaction() as tx:
        tx.execute("UPDATE memories SET activation=5 WHERE id=?", (old["id"],))
    clock.advance(seconds=1)
    rumor = event(kernel, clock, sender="bob", text="Alice 肯定喜欢热闹")
    with pytest.raises(Conflict):
        remember(kernel, rumor, text="喜欢热闹", supersedes=old["id"], expected_revision=1)
    own = event(kernel, clock, text="我现在更喜欢热闹了")
    new = remember(
        kernel, own, text="现在喜欢热闹", tier="long", supersedes=old["id"], expected_revision=1
    )
    assert kernel.memory.profile("qq/alice", "private:alice")["items"][0]["id"] == new["id"]
    assert kernel.memory.get(old["id"])["status"] == "superseded"
    assert kernel.memory.get(old["id"])["superseded_by"] == new["id"]


def test_expiry_does_not_extend_from_reads_and_blocks_old_queued_reply(kernel, clock):
    item = remember(
        kernel,
        event(kernel, clock, text="今天下午有空"),
        kind="current_state",
        valid_until=clock.now() + timedelta(seconds=10),
    )
    task = activity(kernel)
    kernel.memory.search(audience="private:alice", activity_id=task, select=True)
    reply = kernel.outbox.enqueue(
        "expiry", "private:alice", {"text": "你现在有空"}, activity_id=task
    )
    clock.advance(seconds=11)
    assert kernel.memory.search(audience="private:alice") == []
    with pytest.raises(ContextInvalidated):
        kernel.outbox.claim(reply["id"])
    assert kernel.memory.get(item["id"])["valid_until"] < clock.now().isoformat()


def test_derived_memory_cannot_turn_temporary_state_into_permanent_fact(kernel, clock):
    parent = remember(
        kernel,
        event(kernel, clock),
        kind="current_state",
        valid_until=clock.now() + timedelta(hours=1),
    )
    proposal = MemoryProposal(
        kind="fact",
        subjects=["qq/alice"],
        content="当前近况的摘要",
        tier="long",
        source_refs=["memory:" + parent["id"]],
    )
    child = kernel.memory.propose(proposal, operator=True)
    assert child["valid_until"] == parent["valid_until"]
    clock.advance(seconds=3601)
    assert kernel.memory.profile("qq/alice", "private:alice")["items"] == []
    with pytest.raises(Conflict, match="过期"):
        kernel.memory.propose(proposal, operator=True)


def test_private_activity_cannot_retarget_its_output_to_a_group(kernel, clock):
    remember(kernel, event(kernel, clock))
    task = activity(kernel)
    kernel.agent._initial_context(kernel.activities.get(task))
    with pytest.raises(Conflict, match="跨会话"):
        kernel.outbox.enqueue(
            "wrong-target", "group:friends", {"text": "私聊内容"}, activity_id=task
        )
    assert kernel.outbox.list() == []


async def test_model_memory_proposals_reject_missing_sources_without_unknown_work(kernel):
    task = activity(kernel)
    result = await kernel.plugins.execute(
        task,
        "bad-memory-source",
        ToolCall(
            id="remember",
            name="memory__remember",
            arguments={
                "kind": "preference",
                "subjects": ["qq/alice"],
                "content": "不存在的记忆",
                "source_refs": ["event:invented"],
            },
        ),
    )
    assert not result.ok
    assert kernel.memory.list() == []
    assert kernel.lifecycle.state()["work"] == []


async def test_agent_can_remember_from_real_input_and_continue_to_single_reply(kernel, clock):
    source = event(kernel, clock)
    provider = ScriptedProvider(
        ModelReply(
            tool_calls=[
                ToolCall(
                    id="remember-one",
                    name="memory__remember",
                    arguments={
                        "kind": "preference",
                        "subjects": ["qq/alice"],
                        "content": "喜欢安静",
                        "tier": "long",
                        "source_refs": ["event:" + source],
                    },
                )
            ]
        ),
        answer("记下了，下次一起找个安静的地方。"),
    )
    task = configured(kernel, provider)
    with kernel.db.transaction() as tx:
        tx.execute("UPDATE activities SET conversation_id='private:alice' WHERE id=?", (task,))
    result = await kernel.agent.run(task)
    assert result["status"] == "completed"
    assert len(kernel.outbox.list()) == 1
    item = kernel.memory.list()[0]
    assert item["confidence"] == "self_report"
    assert item["activation"] == 0
    assert kernel.memory.references(task) == [{"id": item["id"], "revision": 1}]
    edited = kernel.memory.revise(
        item["id"], MemoryEdit(expected_revision=1, content="人工核实后的偏好")
    )
    assert edited["confidence"] == "operator"
    assert kernel.outbox.list()[0]["status"] == "cancelled"


async def test_forget_clears_derivatives_plugin_cache_drafts_and_prevents_resurrection(
    kernel, clock
):
    source = event(kernel, clock, text="私密的经历")
    item = remember(kernel, source, text="私密的经历")
    child = kernel.memory.propose(
        MemoryProposal(
            kind="experience",
            subjects=["self"],
            content="听过一段私密经历",
            source_refs=["memory:" + item["id"]],
        ),
        operator=True,
    )
    task = activity(kernel)
    kernel.memory.search(audience="private:alice", activity_id=task, select=True)
    await kernel.plugins.execute(
        task,
        "draft",
        ToolCall(
            id="note", name="notebook__write", arguments={"key": "draft", "text": "私密的经历"}
        ),
    )
    delivery = kernel.outbox.enqueue(
        "secret-reply", "private:alice", {"text": "私密的经历"}, activity_id=task
    )
    result = kernel.memory.forget(item["id"], 1)
    assert set(result["affected_ids"]) == {item["id"], child["id"]}
    assert all(row["content"] == "" for row in kernel.memory.list())
    assert kernel.db.rows("SELECT * FROM plugin_values") == []
    assert kernel.outbox.get(delivery["id"])["status"] == "cancelled"
    assert kernel.outbox.get(delivery["id"])["payload"] == {}
    assert all(
        "私密" not in json.dumps(x, ensure_ascii=False) for x in kernel.memory.history(item["id"])
    )
    with pytest.raises(Conflict, match="遗忘"):
        remember(kernel, source)
    assert kernel.inbox.list()[0]["payload"]["text"] == "私密的经历"
    new_task = activity(kernel)
    messages = kernel.agent._initial_context(kernel.activities.get(new_task))
    assert not any("私密的经历" in message.content for message in messages)


async def test_forget_while_model_is_running_discards_late_result_and_still_accounts_cost(
    kernel, clock
):
    memory = remember(kernel, event(kernel, clock), tier="long")
    started, release = asyncio.Event(), asyncio.Event()

    async def delayed():
        started.set()
        await release.wait()
        return answer("旧私密回复")

    provider = ScriptedProvider(delayed)
    task_id = configured(kernel, provider)
    with kernel.db.transaction() as tx:
        tx.execute("UPDATE activities SET conversation_id='private:alice' WHERE id=?", (task_id,))
    task = asyncio.create_task(kernel.agent.run(task_id))
    await started.wait()
    kernel.memory.forget(memory["id"], 1)
    release.set()
    assert (await task)["status"] == "paused"
    assert kernel.outbox.list() == []
    assert kernel.activities.get(task_id)["checkpoint"] == {}
    call = kernel.models.calls()[0]
    assert call["status"] == "invalidated"
    assert call["calculated_micro_usd"] == 20
    assert kernel.db.rows("SELECT response FROM model_calls")[0]["response"] is None


async def test_forget_reaches_raw_context_and_plugin_cache_before_memory_existed(kernel, clock):
    source = event(kernel, clock, text="这是一段以后要忘记的私聊")
    task = activity(kernel)
    messages = kernel.agent._initial_context(kernel.activities.get(task))
    assert any("以后要忘记" in message.content for message in messages)
    assert kernel.memory.references(task) == []
    await kernel.plugins.execute(
        task,
        "draft-before-memory",
        ToolCall(
            id="note",
            name="notebook__write",
            arguments={"key": "draft", "text": "旧私聊摘要"},
        ),
    )
    pending = kernel.outbox.enqueue(
        "raw-draft", "private:alice", {"text": "旧回复"}, activity_id=task
    )
    item = remember(kernel, source)
    kernel.memory.forget(item["id"], 1)
    assert kernel.outbox.get(pending["id"])["status"] == "cancelled"
    assert kernel.db.rows("SELECT * FROM plugin_values") == []
    with pytest.raises(ContextInvalidated):
        kernel.memory.assert_context(task)


def test_sleep_only_persists_new_events_and_does_not_reinforce_or_propose(kernel, clock):
    item = remember(kernel, event(kernel, clock))
    wake = clock.now() + timedelta(hours=1)
    kernel.lifecycle.request_sleep(SleepRequest(earliest_wake_at=wake, latest_wake_at=wake))
    task_id = kernel.activities.create(
        ActivityRequest(kind="memory", goal="睡眠测试", conversation_id="private:alice")
    )["id"]
    event_id = event(kernel, clock, text="睡眠期间新消息")
    with pytest.raises(AdmissionDenied):
        kernel.memory.search(audience="private:alice", activity_id=task_id, select=True)
    with pytest.raises(AdmissionDenied):
        kernel.memory.propose(
            MemoryProposal(
                kind="fact",
                subjects=["qq/alice"],
                content="新的信息",
                source_refs=["event:" + event_id],
            ),
            activity_id=task_id,
        )
    assert kernel.memory.commit_uses(task_id) == 0
    assert kernel.memory.get(item["id"])["activation"] == 0
    assert len(kernel.memory.list()) == 1


def test_memory_and_versions_survive_restart_without_profile_nickname_merging(tmp_path, clock):
    directory = tmp_path / "memory-restart"
    instance = Kernel(directory, clock)
    item = remember(instance, event(instance, clock), tier="long")
    instance.memory.revise(
        item["id"], MemoryEdit(expected_revision=1, content="喜欢安静的阅读环境")
    )
    instance.close()
    restored = Kernel(directory, clock)
    try:
        assert restored.memory.get(item["id"])["revision"] == 2
        assert len(restored.memory.history(item["id"])) == 2
        assert restored.memory.profile("qq/alice", "private:alice")["items"]
        assert restored.memory.profile("different-platform/alice", "private:alice")["items"] == []
    finally:
        restored.close()
