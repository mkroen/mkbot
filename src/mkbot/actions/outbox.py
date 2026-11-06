"""投递与生成解耦。未知是否成功的发送不会自动重试。"""

from __future__ import annotations

import json
from typing import Any, Protocol
from uuid import uuid4

from mkbot.domain.errors import AdmissionDenied, Conflict, NotFound
from mkbot.runtime.clock import timestamp
from mkbot.runtime.lifecycle import Lifecycle, audit
from mkbot.storage.database import encode


class Sender(Protocol):
    async def send(self, conversation_id: str, payload: dict, action_key: str) -> dict: ...


class DeliveryRejected(Exception):
    """适配器能够证明请求未被平台接受时使用。"""


class Outbox:
    def __init__(self, lifecycle: Lifecycle, *, max_attempts: int = 3) -> None:
        self.lifecycle, self.db, self.clock = lifecycle, lifecycle.db, lifecycle.clock
        self.max_attempts = max_attempts
        self.context_validator = None
        self.context_provider = None
        self.on_sent = None

    @staticmethod
    def _check_audience(tx, activity_id: str | None, conversation_id: str) -> None:
        if activity_id is None:
            return  # 操作者/适配器直接投递不复用角色上下文。
        row = tx.execute(
            "SELECT conversation_id FROM activities WHERE id=?", (activity_id,)
        ).fetchone()
        if not row:
            raise NotFound("投递活动不存在")
        if row[0] != conversation_id:
            raise Conflict("跨会话分享需要按目标受众新建活动，不能直接转发当前私聊上下文")

    def enqueue(
        self,
        action_key: str,
        conversation_id: str,
        payload: dict[str, Any],
        *,
        activity_id: str | None = None,
    ) -> dict:
        if not action_key or not conversation_id:
            raise ValueError("投递需要行动 ID 与目标会话")
        content, now = encode(payload), timestamp(self.clock.now())
        refs = self.context_provider(activity_id) if activity_id and self.context_provider else []
        if activity_id and self.context_validator:
            self.context_validator(activity_id, refs)
        with self.db.transaction() as tx:
            self._check_audience(tx, activity_id, conversation_id)
            existing = tx.execute(
                "SELECT * FROM deliveries WHERE action_key=?", (action_key,)
            ).fetchone()
            if existing:
                if (
                    existing["conversation_id"] != conversation_id
                    or existing["payload"] != content
                    or existing["activity_id"] != activity_id
                ):
                    raise Conflict("同一行动 ID 不能对应不同投递")
                return self.get(existing["id"])
            self.lifecycle.check_admission(tx, activity_id)
            delivery_id = str(uuid4())
            tx.execute(
                "INSERT INTO deliveries(id,action_key,activity_id,conversation_id,payload,"
                "created_at,updated_at,memory_refs) VALUES(?,?,?,?,?,?,?,?)",
                (
                    delivery_id,
                    action_key,
                    activity_id,
                    conversation_id,
                    content,
                    now,
                    now,
                    encode(refs),
                ),
            )
            audit(tx, "delivery.queued", delivery_id, {"action_key": action_key}, now)
        return self.get(delivery_id)

    def get(self, delivery_id: str) -> dict:
        rows = self.db.rows("SELECT * FROM deliveries WHERE id=?", (delivery_id,))
        if not rows:
            raise NotFound("投递不存在")
        row = rows[0]
        return row | {
            "payload": json.loads(row["payload"]),
            "receipt": json.loads(row["receipt"]) if row["receipt"] else None,
            "memory_refs": json.loads(row["memory_refs"]),
        }

    def list(self) -> list[dict]:
        return [
            self.get(row["id"])
            for row in self.db.rows("SELECT id FROM deliveries ORDER BY created_at DESC LIMIT 200")
        ]

    def claim(self, delivery_id: str) -> dict:
        now, work_id = timestamp(self.clock.now()), str(uuid4())
        snapshot = self.get(delivery_id)
        if snapshot["activity_id"] and self.context_validator:
            self.context_validator(snapshot["activity_id"], snapshot["memory_refs"])
        with self.db.transaction() as tx:
            row = tx.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            if not row:
                raise NotFound("投递不存在")
            self._check_audience(tx, row["activity_id"], row["conversation_id"])
            state = tx.execute("SELECT * FROM characters WHERE id='main'").fetchone()
            if state["paused"] or state["phase"] == "sleeping":
                raise AdmissionDenied("角色暂停或睡眠期间不能投递")
            # 已接取的投递属于收尾工作，可以在 winding_down 阶段完成。
            if row["status"] != "pending" or row["attempts"] >= self.max_attempts:
                raise AdmissionDenied("投递状态或累计次数不允许再次发送")
            tx.execute(
                "INSERT INTO work(id,activity_id,kind,status,started_at) "
                "VALUES(?,?,'delivery','running',?)",
                (work_id, row["activity_id"], now),
            )
            tx.execute(
                "UPDATE deliveries SET status='sending',work_id=?,attempts=attempts+1,updated_at=? "
                "WHERE id=?",
                (work_id, now, delivery_id),
            )
            audit(tx, "delivery.claimed", delivery_id, {"work_id": work_id}, now)
        return self.get(delivery_id)

    def settle(
        self, delivery_id: str, status: str, *, receipt: dict | None = None, error: str = ""
    ) -> dict:
        if status not in ("sent", "failed", "unknown"):
            raise ValueError("不允许的投递结果")
        if status == "sent" and not receipt:
            raise ValueError("成功投递需要可核对的回执")
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            row = tx.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            if not row:
                raise NotFound("投递不存在")
            if row["status"] not in ("sending", "unknown"):
                if row["status"] == status and row["receipt"] == (
                    encode(receipt) if receipt else None
                ):
                    return self.get(delivery_id)
                raise Conflict("该投递不是待结算状态")
            tx.execute(
                "UPDATE deliveries SET status=?,receipt=?,error=?,updated_at=? WHERE id=?",
                (status, encode(receipt) if receipt else None, error, now, delivery_id),
            )
            work_status = {"sent": "succeeded", "failed": "failed", "unknown": "unknown"}[status]
            tx.execute(
                "UPDATE work SET status=?,detail=?,finished_at=? WHERE id=?",
                (work_status, error, now, row["work_id"]),
            )
            audit(tx, "delivery.settled", delivery_id, {"status": status}, now)
            self.lifecycle.try_sleep(tx)
        if status == "sent" and row["activity_id"] and self.on_sent:
            # 回执已确定；记忆规则变更不能撤回平台已确认的发送。
            from mkbot.domain.errors import ContextInvalidated

            try:
                self.on_sent(row["activity_id"])
            except ContextInvalidated:
                pass
        return self.get(delivery_id)

    def retry(self, delivery_id: str) -> None:
        with self.db.transaction() as tx:
            self.lifecycle.check_admission(tx)
            row = tx.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            if not row or row["status"] != "failed" or row["attempts"] >= self.max_attempts:
                raise AdmissionDenied("只能重试已确认失败且未超过上限的投递")
            tx.execute("UPDATE deliveries SET status='pending' WHERE id=?", (delivery_id,))
            audit(tx, "delivery.retry", delivery_id, {}, timestamp(self.clock.now()))

    async def send(self, delivery_id: str, sender: Sender) -> dict:
        delivery = self.claim(delivery_id)
        try:
            receipt = await sender.send(
                delivery["conversation_id"], delivery["payload"], delivery["action_key"]
            )
        except DeliveryRejected as exc:
            return self.settle(delivery_id, "failed", error=type(exc).__name__)
        except BaseException as exc:
            self.settle(delivery_id, "unknown", error=type(exc).__name__)
            raise
        if not receipt:
            return self.settle(delivery_id, "unknown", error="平台未提供确认回执")
        return self.settle(delivery_id, "sent", receipt=receipt)
