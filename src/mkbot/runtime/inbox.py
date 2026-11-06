"""持久收件与审阅决策。入站路径不会调用模型。"""

from __future__ import annotations

import json
from collections import OrderedDict
from uuid import uuid4

from mkbot.domain.errors import Conflict
from mkbot.domain.models import InboundEvent, ReviewDecision
from mkbot.runtime.clock import timestamp
from mkbot.runtime.lifecycle import Lifecycle, audit
from mkbot.storage.database import encode


class InboxService:
    def __init__(self, lifecycle: Lifecycle) -> None:
        self.lifecycle, self.db, self.clock = lifecycle, lifecycle.db, lifecycle.clock

    def receive(self, event: InboundEvent) -> dict:
        now = timestamp(self.clock.now())
        payload = encode(event.model_dump(mode="json"))
        with self.db.transaction() as tx:
            previous = tx.execute(
                "SELECT id,payload FROM events WHERE source=? AND external_id=?",
                (event.source, event.external_id),
            ).fetchone()
            if previous:
                if previous["payload"] != payload:
                    raise Conflict("同一平台事件 ID 对应不同内容")
                return {"id": previous["id"], "duplicate": True}
            event_id = str(uuid4())
            tx.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    event.source,
                    event.external_id,
                    event.conversation_id,
                    event.sender_id,
                    event.conversation_kind,
                    payload,
                    timestamp(event.occurred_at),
                    now,
                ),
            )
            self_id = tx.execute("SELECT self_id FROM characters WHERE id='main'").fetchone()[0]
            status = "ignored" if event.sender_id == self_id else "pending"
            tx.execute("INSERT INTO inbox(event_id,status) VALUES(?,?)", (event_id, status))
            audit(tx, "inbox.received", event_id, {"status": status}, now)
        return {"id": event_id, "duplicate": False, "status": status}

    def list(self, *, limit: int = 100) -> list[dict]:
        limit = max(1, min(limit, 500))
        rows = self.db.rows(
            "SELECT e.*,i.status,i.decision,i.reason,i.reviewed_at,i.revisit_at "
            "FROM inbox i JOIN events e ON e.id=i.event_id ORDER BY e.received_at DESC LIMIT ?",
            (limit,),
        )
        return [row | {"payload": json.loads(row["payload"])} for row in rows]

    def batches(self, *, limit: int = 100) -> list[dict]:
        """每个会话独立成批，不把不同私聊混入一个 Prompt。"""
        limit = max(1, min(limit, 500))
        rows = self.db.rows(
            "SELECT e.* FROM inbox i JOIN events e ON e.id=i.event_id "
            "WHERE i.status='pending' OR (i.status='deferred' AND i.revisit_at<=?) "
            "ORDER BY e.received_at,e.rowid LIMIT ?",
            (timestamp(self.clock.now()), limit),
        )
        groups: OrderedDict[str, list[dict]] = OrderedDict()
        for row in rows:
            groups.setdefault(row["conversation_id"], []).append(
                row | {"payload": json.loads(row["payload"])}
            )
        return [{"conversation_id": key, "events": events} for key, events in groups.items()]

    def resolve(self, decision: ReviewDecision) -> None:
        now = timestamp(self.clock.now())
        if decision.revisit_at and timestamp(decision.revisit_at) <= now:
            raise ValueError("再评估时间必须在未来")
        ids = list(dict.fromkeys(decision.event_ids))
        placeholders = ",".join("?" for _ in ids)
        with self.db.transaction() as tx:
            self.lifecycle.check_admission(tx)
            rows = tx.execute(
                f"SELECT i.*,e.conversation_id FROM inbox i JOIN events e ON e.id=i.event_id "
                f"WHERE event_id IN ({placeholders})",
                ids,
            ).fetchall()
            if len(rows) != len(ids) or len({r["conversation_id"] for r in rows}) != 1:
                raise Conflict("审阅对象不存在或跨越了不同会话")
            if any(r["status"] not in ("pending", "deferred") for r in rows):
                raise Conflict("部分留言已完成审阅")
            tx.executemany(
                "UPDATE inbox SET status=?,decision=?,reason=?,reviewed_at=?,revisit_at=? "
                "WHERE event_id=?",
                [
                    (
                        "deferred" if decision.decision == "defer" else "reviewed",
                        decision.decision,
                        decision.reason,
                        now,
                        timestamp(decision.revisit_at) if decision.revisit_at else None,
                        event_id,
                    )
                    for event_id in ids
                ],
            )
            audit(tx, "inbox.reviewed", ids[0], decision.model_dump(mode="json"), now)
