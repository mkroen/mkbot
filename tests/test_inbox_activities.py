from datetime import timedelta

import pytest

from mkbot.domain.errors import AdmissionDenied, Conflict
from mkbot.domain.models import ActivityRequest, InboundEvent, ReviewDecision


def event(clock, external_id="e1", conversation="private:a", sender="a", text="你好"):
    return InboundEvent(
        source="test",
        external_id=external_id,
        conversation_id=conversation,
        sender_id=sender,
        text=text,
        occurred_at=clock.now(),
    )


def test_replayed_event_is_deduplicated_and_collision_rejected(kernel, clock):
    first = kernel.inbox.receive(event(clock))
    again = kernel.inbox.receive(event(clock))
    assert first["id"] == again["id"] and again["duplicate"]
    assert len(kernel.inbox.list()) == 1
    with pytest.raises(Conflict):
        kernel.inbox.receive(event(clock, text="另一条内容"))


def test_own_message_does_not_create_review_candidate(kernel, clock):
    kernel.inbox.receive(event(clock, sender="mkbot"))
    assert kernel.inbox.batches() == []
    assert kernel.inbox.list()[0]["status"] == "ignored"


def test_batching_preserves_private_boundaries(kernel, clock):
    ids = [
        kernel.inbox.receive(event(clock, "a1"))["id"],
        kernel.inbox.receive(event(clock, "a2", text="后面不用了"))["id"],
    ]
    other = kernel.inbox.receive(event(clock, "b1", "private:b", "b"))["id"]
    assert sorted(len(b["events"]) for b in kernel.inbox.batches()) == [1, 2]
    with pytest.raises(Conflict):
        kernel.inbox.resolve(
            ReviewDecision(event_ids=[ids[0], other], decision="no_reply", reason="混合")
        )
    kernel.inbox.resolve(ReviewDecision(event_ids=ids, decision="no_reply", reason="请求已取消"))
    assert len(kernel.inbox.batches()) == 1
    assert kernel.outbox.list() == []


def test_defer_has_a_bounded_revisit_trigger(kernel, clock):
    received = kernel.inbox.receive(event(clock))
    kernel.inbox.resolve(
        ReviewDecision(
            event_ids=[received["id"]],
            decision="defer",
            reason="稍后看",
            revisit_at=clock.now() + timedelta(minutes=30),
        )
    )
    assert kernel.inbox.batches() == []
    clock.advance(seconds=1800)
    assert len(kernel.inbox.batches()) == 1
    kernel.inbox.resolve(
        ReviewDecision(
            event_ids=[received["id"]],
            decision="expired",
            reason="已过时",
        )
    )
    assert kernel.inbox.batches() == []


def test_steps_and_checkpoint_survive_pause_and_descendant(kernel):
    parent = kernel.activities.create(ActivityRequest(kind="read", goal="阅读主题"))
    active = kernel.activities.claim(parent["id"])
    assert kernel.activities.step(parent["id"], max_steps=2) == 1
    checkpoint = kernel.activities.checkpoint(parent["id"], active["version"], {"page": 3})
    with pytest.raises(Conflict):
        kernel.activities.checkpoint(parent["id"], active["version"], {"page": 1})
    kernel.activities.transition(parent["id"], "paused")
    resumed = kernel.activities.claim(parent["id"])
    assert resumed["checkpoint"] == checkpoint["checkpoint"]
    child = kernel.activities.create(
        ActivityRequest(kind="research", goal="跟进", parent_id=parent["id"])
    )
    kernel.activities.claim(child["id"])
    assert kernel.activities.step(child["id"], max_steps=2) == 2
    with pytest.raises(AdmissionDenied):
        kernel.activities.step(parent["id"], max_steps=2)
