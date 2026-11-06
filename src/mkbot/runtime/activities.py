"""活动检查点和状态转换，未来目标不会占用正在执行的工作槽位。"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from mkbot.domain.errors import AdmissionDenied, Conflict, NotFound
from mkbot.domain.models import ActivityRequest
from mkbot.runtime.clock import timestamp
from mkbot.runtime.lifecycle import Lifecycle, audit
from mkbot.storage.database import encode


class ActivityService:
    def __init__(self, lifecycle: Lifecycle) -> None:
        self.lifecycle, self.db, self.clock = lifecycle, lifecycle.db, lifecycle.clock

    def create(self, request: ActivityRequest) -> dict:
        with self.db.transaction() as tx:
            activity_id = self.create_in(tx, request)
        return self.get(activity_id)

    def create_in(self, tx, request: ActivityRequest) -> str:
        """由调度器在领取事件的同一事务中建立活动。"""
        activity_id, now = str(uuid4()), timestamp(self.clock.now())
        root_id = activity_id
        triggers = sorted(set(request.trigger_event_ids))
        for event_id in triggers:
            event = tx.execute(
                "SELECT conversation_id FROM events WHERE id=?", (event_id,)
            ).fetchone()
            if not event or event[0] != request.conversation_id:
                raise Conflict("触发事件必须属于活动所在会话")
        if request.parent_id:
            parent = tx.execute(
                "SELECT root_id FROM activities WHERE id=?", (request.parent_id,)
            ).fetchone()
            if not parent:
                raise NotFound("父活动不存在")
            root_id = parent["root_id"]
        tx.execute(
            "INSERT INTO activities(id,root_id,parent_id,kind,goal,origin,conversation_id,"
            "priority,due_at,created_at,updated_at,trigger_event_ids) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                activity_id,
                root_id,
                request.parent_id,
                request.kind,
                request.goal,
                request.origin,
                request.conversation_id,
                request.priority,
                timestamp(request.due_at) if request.due_at else None,
                now,
                now,
                encode(triggers),
            ),
        )
        audit(tx, "activity.created", activity_id, {"root_id": root_id}, now)
        return activity_id

    def get(self, activity_id: str) -> dict:
        rows = self.db.rows("SELECT * FROM activities WHERE id=?", (activity_id,))
        if not rows:
            raise NotFound("活动不存在")
        return rows[0] | {
            "checkpoint": json.loads(rows[0]["checkpoint"]),
            "trigger_event_ids": json.loads(rows[0]["trigger_event_ids"]),
        }

    def list(self) -> list[dict]:
        return [
            row
            | {
                "checkpoint": json.loads(row["checkpoint"]),
                "trigger_event_ids": json.loads(row["trigger_event_ids"]),
            }
            for row in self.db.rows("SELECT * FROM activities ORDER BY created_at DESC LIMIT 200")
        ]

    def claim(self, activity_id: str) -> dict:
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            self.lifecycle.check_admission(tx)
            item = tx.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
            if not item:
                raise NotFound("活动不存在")
            if item["status"] not in ("queued", "waiting", "paused", "interrupted"):
                raise Conflict("当前活动不能接取")
            if item["due_at"] and item["due_at"] > now:
                raise AdmissionDenied("活动尚未到执行时间")
            unresolved = tx.execute(
                "SELECT count(*) FROM work WHERE activity_id=? AND status IN ('running','unknown')",
                (activity_id,),
            ).fetchone()[0]
            if unresolved:
                raise AdmissionDenied("旧工作尚未完成或核对，不能重跑活动")
            tx.execute(
                "UPDATE activities SET status='running',version=version+1,updated_at=? WHERE id=?",
                (now, activity_id),
            )
            audit(tx, "activity.claimed", activity_id, {}, now)
        return self.get(activity_id)

    def checkpoint(self, activity_id: str, expected_version: int, data: dict[str, Any]) -> dict:
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            changed = tx.execute(
                "UPDATE activities SET checkpoint=?,version=version+1,updated_at=? "
                "WHERE id=? AND version=? AND status IN ('running','waiting','paused')",
                (encode(data), now, activity_id, expected_version),
            ).rowcount
            if not changed:
                raise Conflict("活动版本已变化或当前状态不允许写检查点")
            audit(tx, "activity.checkpoint", activity_id, {"version": expected_version + 1}, now)
        return self.get(activity_id)

    def transition(self, activity_id: str, status: str) -> dict:
        if status not in ("paused", "waiting", "completed", "cancelled", "failed"):
            raise ValueError("不允许的活动状态转换")
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            item = tx.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
            if not item:
                raise NotFound("活动不存在")
            if item["status"] in ("completed", "cancelled", "failed"):
                if item["status"] == status:
                    return self.get(activity_id)
                raise Conflict("已结束的活动不能重新改写状态")
            unresolved = tx.execute(
                "SELECT count(*) FROM work WHERE activity_id=? AND status IN ('running','unknown')",
                (activity_id,),
            ).fetchone()[0]
            if unresolved:
                raise AdmissionDenied("请先完成或核对正在执行的工作")
            tx.execute(
                "UPDATE activities SET status=?,version=version+1,updated_at=? WHERE id=?",
                (status, now, activity_id),
            )
            audit(tx, "activity.transition", activity_id, {"status": status}, now)
            self.lifecycle.try_sleep(tx)
        return self.get(activity_id)

    def step(self, activity_id: str, *, max_steps: int) -> int:
        """派生任务和恢复后的活动累计同一个根活动步骤额度。"""
        if max_steps < 1:
            raise ValueError("最大步骤数必须为正数")
        with self.db.transaction() as tx:
            self.lifecycle.check_admission(tx, activity_id)
            item = tx.execute(
                "SELECT root_id FROM activities WHERE id=?", (activity_id,)
            ).fetchone()
            root = tx.execute(
                "SELECT steps FROM activities WHERE id=?", (item["root_id"],)
            ).fetchone()
            if root["steps"] >= max_steps:
                raise AdmissionDenied("活动累计步骤已达到上限")
            tx.execute("UPDATE activities SET steps=steps+1 WHERE id=?", (item["root_id"],))
            return root["steps"] + 1
