from datetime import timedelta

import pytest

from mkbot.actions.outbox import DeliveryRejected
from mkbot.domain.errors import AdmissionDenied, Conflict
from mkbot.domain.models import SleepRequest
from mkbot.runtime.kernel import Kernel


class RecordingSender:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def send(self, conversation_id, payload, action_key):
        self.calls.append((conversation_id, payload, action_key))
        if self.error:
            raise self.error
        return {"message_id": "platform-1"}


async def test_pending_delivery_drains_before_sleep(kernel, clock):
    item = kernel.outbox.enqueue("action-one", "private:a", {"text": "晚安"})
    state = kernel.lifecycle.request_sleep(
        SleepRequest(
            earliest_wake_at=clock.now() + timedelta(hours=8),
            latest_wake_at=clock.now() + timedelta(hours=8),
        )
    )
    assert state["phase"] == "winding_down"
    sender = RecordingSender()
    result = await kernel.outbox.send(item["id"], sender)
    assert result["status"] == "sent"
    assert len(sender.calls) == 1
    assert kernel.lifecycle.state()["phase"] == "sleeping"
    repeated = kernel.outbox.enqueue("action-one", "private:a", {"text": "晚安"})
    assert repeated["id"] == item["id"]
    with pytest.raises(AdmissionDenied):
        await kernel.outbox.send(item["id"], sender)
    assert len(sender.calls) == 1


async def test_timeout_is_unknown_not_automatic_retry(kernel):
    item = kernel.outbox.enqueue("action-one", "private:a", {"text": "可能已经到了"})
    sender = RecordingSender(TimeoutError())
    with pytest.raises(TimeoutError):
        await kernel.outbox.send(item["id"], sender)
    assert kernel.outbox.get(item["id"])["status"] == "unknown"
    with pytest.raises(AdmissionDenied):
        kernel.outbox.retry(item["id"])
    assert len(sender.calls) == 1
    kernel.outbox.settle(item["id"], "sent", receipt={"message_id": "confirmed"})
    assert kernel.outbox.get(item["id"])["status"] == "sent"


async def test_confirmed_rejection_can_retry_but_is_bounded(kernel):
    item = kernel.outbox.enqueue("action-one", "private:a", {"text": "发送"})
    sender = RecordingSender(DeliveryRejected())
    for attempt in range(3):
        assert (await kernel.outbox.send(item["id"], sender))["status"] == "failed"
        if attempt < 2:
            kernel.outbox.retry(item["id"])
    with pytest.raises(AdmissionDenied):
        kernel.outbox.retry(item["id"])
    assert len(sender.calls) == 3


def test_action_key_cannot_change_recipient_or_content(kernel):
    kernel.outbox.enqueue("action-one", "private:a", {"text": "私密"})
    with pytest.raises(Conflict):
        kernel.outbox.enqueue("action-one", "group:x", {"text": "私密"})


def test_restart_during_send_requires_receipt_reconciliation(tmp_path, clock):
    first = Kernel(tmp_path, clock)
    item = first.outbox.enqueue("action-one", "private:a", {"text": "发送中"})
    first.outbox.claim(item["id"])
    first.close()
    second = Kernel(tmp_path, clock)
    try:
        assert second.outbox.get(item["id"])["status"] == "unknown"
        with pytest.raises(AdmissionDenied):
            second.outbox.claim(item["id"])
    finally:
        second.close()
