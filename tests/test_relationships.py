import json
from datetime import timedelta
from uuid import uuid4

import pytest
from test_memory import activity, event, remember
from test_model_gateway import ScriptedProvider
from test_scheduler import choice, enable, receive, setting

from mkbot.character.relationships import RelationshipChange, RelationshipEdit
from mkbot.domain.errors import AdmissionDenied, Conflict, ContextInvalidated
from mkbot.domain.models import SleepRequest
from mkbot.memory.contracts import ManualMemory, MemoryProposal
from mkbot.models.contracts import ModelReply, ToolCall, Usage
from mkbot.runtime.kernel import Kernel


def reflection(subject="test/alice", **changes):
    return ToolCall(
        id=str(uuid4()),
        name="relationships__reflect",
        arguments={
            "subject_id": subject,
            "reason": "这次相处中对方认真倾听",
            "familiarity": 1,
            "affinity": 2,
            "trust": 1,
            **changes,
        },
    )


async def interact(kernel, clock, *, reply=False, **changes):
    response = ModelReply(
        tool_calls=[reflection(**changes), *choice("reply", text="谢谢你听我说").tool_calls]
        if reply
        else [reflection(**changes), *choice().tool_calls],
        usage=Usage(input_tokens=10, output_tokens=5),
    )
    if "primary" in kernel.models.providers:
        kernel.models.providers["primary"].steps.append(response)
    else:
        provider = ScriptedProvider(response)
        enable(kernel, provider, debounce_seconds=0, conversation_cooldown_seconds=0)
    event_id = receive(kernel, "听你说说今天的事")
    clock.advance(seconds=1)
    result = await kernel.scheduler.step()
    return result["activity_id"], event_id


def baseline(kernel, subject="test/alice", scope=None, **scores):
    rows = kernel.db.rows("SELECT version FROM people WHERE subject_id=?", (subject,))
    return kernel.relationships.edit(
        subject,
        RelationshipEdit(
            expected_version=rows[0]["version"] if rows else 0,
            label="预览人物",
            familiarity=50,
            affinity=20,
            trust=10,
            reason="人工指定的初始关系",
            scope=scope or ["@self"],
            **scores,
        ),
    )


async def test_pending_change_waits_for_delivery_and_reads_do_not_reward(kernel, clock):
    current, _ = await interact(kernel, clock, reply=True)
    person = kernel.relationships.get("test/alice")
    assert person["scores"] == {"familiarity": 0, "affinity": 0, "trust": 0}
    assert person["changes"][0]["status"] == "pending"
    assert kernel.memory.search(audience="private:alice") == []
    delivery = kernel.outbox.list()[0]
    kernel.outbox.claim(delivery["id"])
    kernel.outbox.settle(delivery["id"], "sent", receipt={"id": "message1"})
    saved = kernel.relationships.get("test/alice")
    assert saved["scores"] == {"familiarity": 1, "affinity": 2, "trust": 1}
    for _ in range(20):
        kernel.relationships.get("test/alice")
        kernel.relationships.list()
        kernel.relationships.commit(current)
    assert kernel.relationships.get("test/alice")["scores"] == saved["scores"]
    assert len(kernel.models.calls()) == 1


async def test_no_reply_completes_observation_without_forcing_response(kernel, clock):
    await interact(kernel, clock)
    assert kernel.relationships.get("test/alice")["scores"]["affinity"] == 2
    assert kernel.outbox.list() == []
    assert kernel.inbox.list()[0]["decision"] == "no_reply"


async def test_global_attention_and_scoped_profile_do_not_expose_private_reasons(kernel, clock):
    await interact(kernel, clock, reason="只有私下说过的秘密")
    assert kernel.relationships.get("test/alice")["scores"]["affinity"] == 2
    other = kernel.relationships.get("test/alice", "group:friends")
    assert other["scores"]["affinity"] == 0 and other["changes"] == []
    boundary = kernel.memory.create_manual(
        ManualMemory(
            kind="boundary",
            subjects=["test/alice"],
            content="不喜欢公开提到家事",
            scope=["private:alice"],
        )
    )
    mine = activity(kernel, "private:alice")
    context = kernel.relationships.context(mine, ["test/alice"])
    assert context[0]["scores"]["affinity"] == 2
    assert "秘密" in json.dumps(context, ensure_ascii=False)
    assert boundary["id"] in {row["id"] for row in kernel.memory.references(mine)}
    group = activity(kernel, "group:friends")
    public_context = kernel.relationships.context(group, ["test/alice"])
    assert "秘密" not in json.dumps(public_context, ensure_ascii=False)
    assert "家事" not in json.dumps(public_context, ensure_ascii=False)
    assert kernel.relationships.attention([{"source": "test", "sender": "alice"}]) > 0


async def test_forgetting_relation_evidence_removes_scores_and_invalidates_consumers(kernel, clock):
    await interact(kernel, clock)
    person = kernel.relationships.get("test/alice")
    change = person["changes"][0]
    reader = activity(kernel, "private:alice")
    kernel.relationships.context(reader, ["test/alice"])
    memory = kernel.memory.get(change["memory_id"])
    kernel.memory.forget(memory["id"], memory["revision"])
    assert kernel.relationships.get("test/alice")["scores"]["affinity"] == 0
    with pytest.raises(ContextInvalidated):
        kernel.assert_context(reader)
    assert kernel.relationships.get("test/alice")["version"] > person["version"]


async def test_budget_is_rolling_and_zero_reward_cannot_be_replayed_next_day(kernel, clock):
    setting(kernel, "relationships", affinity_daily=5)
    await interact(kernel, clock, affinity=5)
    _, second_event = await interact(kernel, clock, affinity=5)
    assert kernel.relationships.get("test/alice")["scores"]["affinity"] == 5
    clock.advance(seconds=86401)
    replay = activity(kernel, "private:alice", [second_event])
    with pytest.raises(Conflict, match="重复加分"):
        kernel.relationships.propose(
            replay, RelationshipChange(subject_id="test/alice", reason="重复同一次相处", affinity=5)
        )
    kernel.activities.transition(replay, "completed")
    await interact(kernel, clock, affinity=5)
    assert kernel.relationships.get("test/alice")["scores"]["affinity"] == 10


async def test_positive_negative_changes_share_absolute_daily_limit(kernel, clock):
    await interact(kernel, clock, affinity=5)
    await interact(kernel, clock, affinity=-5)
    await interact(kernel, clock, affinity=5)
    assert kernel.relationships.get("test/alice")["scores"]["affinity"] == 0


def test_real_participant_only_root_dedup_and_independent_people(kernel, clock):
    source = event(kernel, clock)
    current = activity(kernel, triggers=[source])
    proposal = RelationshipChange(subject_id="qq/alice", reason="这次共同经历", affinity=2)
    first = kernel.relationships.propose(current, proposal)
    second = kernel.relationships.propose(current, proposal.model_copy(update={"affinity": 3}))
    assert second["id"] == first["id"] and second["duplicate"]
    with pytest.raises(Conflict, match="真实参与者"):
        kernel.relationships.propose(current, proposal.model_copy(update={"subject_id": "qq/bob"}))
    with pytest.raises(Conflict, match="真实参与者"):
        kernel.relationships.propose(
            current, proposal.model_copy(update={"subject_id": "other/alice"})
        )
    with pytest.raises(Conflict, match="单次上限"):
        kernel.relationships.propose(current, proposal.model_copy(update={"affinity": 10}))


async def test_sleep_defers_relationship_commit_until_awake(kernel, clock):
    _, _ = await interact(kernel, clock, reply=True)
    wake = clock.now() + timedelta(seconds=60)
    kernel.lifecycle.request_sleep(SleepRequest(earliest_wake_at=wake, latest_wake_at=wake))
    delivery = kernel.outbox.list()[0]
    kernel.outbox.claim(delivery["id"])
    kernel.outbox.settle(delivery["id"], "sent", receipt={"id": "sleep1"})
    assert kernel.lifecycle.state()["phase"] == "sleeping"
    kernel.relationships.reconcile()
    assert kernel.relationships.get("test/alice")["scores"]["affinity"] == 0
    receive(kernel, "睡着了吗", conversation="private:bob")
    await kernel.scheduler.step()
    assert len(kernel.relationships.list()) == 1
    clock.advance(seconds=60)
    kernel.lifecycle.tick()
    kernel.relationships.reconcile()
    assert kernel.relationships.get("test/alice")["scores"]["affinity"] == 2


async def test_restart_recovers_pending_change_after_confirmed_receipt(tmp_path, clock):
    path = tmp_path / "relations"
    first = Kernel(path, clock)
    current, _ = await interact(first, clock, reply=True)
    delivery = first.outbox.list()[0]
    first.outbox.claim(delivery["id"])
    first.outbox.on_sent = None
    first.outbox.settle(delivery["id"], "sent", receipt={"id": "confirmed"})
    first.close()
    second = Kernel(path, clock)
    try:
        second.relationships.reconcile()
        assert second.relationships.get("test/alice")["scores"]["affinity"] == 2
        assert second.relationships.commit(current) == 0
    finally:
        second.close()


async def test_manual_baseline_replaces_old_totals_and_pending_proposal(kernel, clock):
    await interact(kernel, clock, reply=True)
    old = kernel.relationships.get("test/alice")
    saved = baseline(kernel)
    kernel.relationships.reconcile()
    assert saved["scores"]["affinity"] == 20
    assert any(
        row["status"] == "discarded" for row in kernel.relationships.get("test/alice")["changes"]
    )
    with pytest.raises(Conflict):
        kernel.relationships.edit(
            "test/alice",
            RelationshipEdit(
                expected_version=old["version"],
                familiarity=0,
                affinity=0,
                trust=0,
                reason="旧版本改写",
            ),
        )
    assert kernel.relationships.get("test/alice", "group:all")["scores"]["affinity"] == 0


def test_attention_bonus_is_bounded_and_does_not_starve_unknown_people(kernel, clock):
    kernel.relationships.edit(
        "test/alice",
        RelationshipEdit(
            expected_version=0, familiarity=100, affinity=100, trust=100, reason="测试熟人"
        ),
    )
    receive(kernel, "陌生人的较早请求", conversation="private:bob")
    clock.advance(seconds=901)
    receive(kernel, "熟人的表情", conversation="private:alice")
    assert kernel.scheduler._candidates()[0]["conversation_id"] == "private:bob"
    receive(
        kernel, "明确找角色", conversation="group:new", conversation_kind="group", directed=True
    )
    assert kernel.scheduler._candidates()[0]["conversation_id"] == "group:new"
    for row in kernel.scheduler._candidates():
        assert 0 <= row["relationship_bonus"] <= 10


def test_model_memory_cannot_launder_private_context_by_only_citing_public_fact(kernel, clock):
    private_source = event(kernel, clock, text="我的私人信息")
    public_source = event(
        kernel, clock, conversation="group:public", kind="group", text="一条公开资料"
    )
    private_memory = remember(kernel, private_source)
    current = activity(kernel)
    kernel.memory.select_refs(current, [{"id": private_memory["id"], "revision": 1}])
    request = MemoryProposal(
        kind="opinion",
        subjects=["self"],
        content="根据全部上下文产生的判断",
        source_refs=["event:" + public_source],
        tier="long",
    )
    inferred = kernel.memory.propose(request, activity_id=current)
    assert inferred["use_scope"] == ["private:alice"]
    assert "event:" + private_source in inferred["source_refs"]
    with pytest.raises(Conflict, match="扩大来源范围"):
        kernel.memory.propose(
            request.model_copy(update={"use_scope": ["*"], "disclosure_scope": ["*"]}),
            activity_id=current,
        )


def test_relationship_tools_reject_sleep_and_no_messages_are_not_interactions(kernel, clock):
    current = activity(kernel)
    request = RelationshipChange(subject_id="qq/alice", reason="没有相处也想加分", familiarity=1)
    with pytest.raises(Conflict):
        kernel.relationships.propose(current, request)
    kernel.activities.transition(current, "completed")
    wake = clock.now() + timedelta(hours=1)
    kernel.lifecycle.request_sleep(SleepRequest(earliest_wake_at=wake, latest_wake_at=wake))
    with pytest.raises(AdmissionDenied):
        kernel.relationships.propose(current, request)
