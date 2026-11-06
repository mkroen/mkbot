"""角色状态与工作准入共用 SQLite 写事务，避免入睡检查与新工作启动竞态。"""

import json
import random
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from mkbot.domain.errors import AdmissionDenied, Conflict, NotFound
from mkbot.domain.models import Phase, SleepRequest
from mkbot.runtime.clock import Clock, timestamp
from mkbot.storage.database import Database, encode

WorkOutcome = Literal["succeeded", "failed", "cancelled", "unknown"]


def audit(tx: sqlite3.Connection, event: str, entity: str, data: dict[str, Any], now: str) -> None:
    tx.execute(
        "INSERT INTO audit(event_type,entity_id,data,occurred_at) VALUES(?,?,?,?)",
        (event, entity, encode(data), now),
    )


class Lifecycle:
    def __init__(self, db: Database, clock: Clock, *, name: str = "mkbot") -> None:
        self.db, self.clock = db, clock
        with db.transaction() as tx:
            tx.execute(
                "INSERT OR IGNORE INTO characters(id,name,self_id,updated_at) VALUES(?,?,?,?)",
                ("main", name, "mkbot", timestamp(clock.now())),
            )

    def state(self) -> dict[str, Any]:
        result = self.db.rows("SELECT * FROM characters WHERE id='main'")[0]
        result["paused"] = bool(result["paused"])
        result["sleep_plan"] = json.loads(result["sleep_plan"]) if result["sleep_plan"] else None
        result["work"] = self.db.rows(
            "SELECT * FROM work WHERE status IN ('running','unknown') ORDER BY started_at"
        )
        result["health"] = (
            "degraded" if any(w["status"] == "unknown" for w in result["work"]) else "healthy"
        )
        return result

    def check_admission(self, tx: sqlite3.Connection, activity_id: str | None = None) -> None:
        state = tx.execute("SELECT * FROM characters WHERE id='main'").fetchone()
        if state["paused"] or state["phase"] == Phase.SLEEPING:
            raise AdmissionDenied("角色已暂停或正在睡眠")
        if activity_id:
            activity = tx.execute(
                "SELECT status FROM activities WHERE id=?", (activity_id,)
            ).fetchone()
            if not activity or activity["status"] != "running":
                raise AdmissionDenied("活动未在执行状态")
        if state["phase"] == Phase.WINDING_DOWN and activity_id is None:
            raise AdmissionDenied("正在收尾，暂不接取新的工作")

    def request_sleep(self, request: SleepRequest, *, fraction: float | None = None) -> dict:
        now = self.clock.now()
        if request.earliest_wake_at <= now:
            raise ValueError("唤醒时间必须在未来")
        fraction = random.random() if fraction is None else fraction
        if not 0 <= fraction <= 1:
            raise ValueError("唤醒窗口选择值须在 0 到 1 之间")
        duration = request.latest_wake_at - request.earliest_wake_at
        wake_at = request.earliest_wake_at + timedelta(seconds=duration.total_seconds() * fraction)
        plan = request.model_dump(mode="json") | {"selected_wake_at": timestamp(wake_at)}
        with self.db.transaction() as tx:
            state = tx.execute("SELECT * FROM characters WHERE id='main'").fetchone()
            if state["paused"] or state["phase"] != Phase.AWAKE:
                raise Conflict("仅清醒且未暂停时可以提出新的睡眠计划")
            tx.execute(
                "UPDATE characters SET phase=?,sleep_plan=?,wake_at=?,"
                "version=version+1,updated_at=? WHERE id='main'",
                (Phase.WINDING_DOWN, encode(plan), timestamp(wake_at), timestamp(now)),
            )
            audit(tx, "sleep.requested", "main", plan, timestamp(now))
            self.try_sleep(tx)
        return self.state()

    def try_sleep(self, tx: sqlite3.Connection) -> bool:
        state = tx.execute("SELECT * FROM characters WHERE id='main'").fetchone()
        if state["paused"] or state["phase"] != Phase.WINDING_DOWN:
            return False
        occupied = tx.execute(
            "SELECT (SELECT count(*) FROM work WHERE status IN ('running','unknown')) + "
            "(SELECT count(*) FROM activities WHERE status='running') + "
            "(SELECT count(*) FROM deliveries WHERE status IN ('pending','sending','unknown'))"
        ).fetchone()[0]
        if occupied:
            return False
        now = timestamp(self.clock.now())
        if state["wake_at"] <= now:
            tx.execute(
                "UPDATE characters SET phase='awake',sleep_plan=NULL,wake_at=NULL,"
                "version=version+1,updated_at=? WHERE id='main'",
                (now,),
            )
            audit(tx, "sleep.window_missed", "main", {}, now)
            return False
        plan = json.loads(state["sleep_plan"])
        plan["sleep_started_at"] = now
        tx.execute(
            "UPDATE characters SET phase='sleeping',sleep_plan=?,version=version+1,"
            "updated_at=? WHERE id='main'",
            (encode(plan), now),
        )
        audit(tx, "sleep.entered", "main", plan, now)
        return True

    def tick(self) -> bool:
        """只推进确定性的作息状态；不执行模型或把积压任务全部补跑。"""
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            state = tx.execute("SELECT * FROM characters WHERE id='main'").fetchone()
            if state["paused"]:
                return False
            if state["phase"] == Phase.SLEEPING and state["wake_at"] <= now:
                self._wake(tx, "timer", now)
                return True
            return self.try_sleep(tx)

    def _wake(self, tx: sqlite3.Connection, reason: str, now: str) -> None:
        tx.execute(
            "UPDATE characters SET phase='waking',sleep_plan=NULL,wake_at=NULL,"
            "version=version+1,updated_at=? WHERE id='main'",
            (now,),
        )
        audit(tx, "sleep.waking", "main", {"reason": reason}, now)

    def wake(self) -> dict:
        with self.db.transaction() as tx:
            state = tx.execute("SELECT * FROM characters WHERE id='main'").fetchone()
            if state["paused"]:
                raise AdmissionDenied("请先解除运维暂停")
            if state["phase"] in (Phase.SLEEPING, Phase.WINDING_DOWN):
                self._wake(tx, "operator", timestamp(self.clock.now()))
        return self.state()

    def finish_waking(self) -> None:
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            changed = tx.execute(
                "UPDATE characters SET phase='awake',version=version+1,updated_at=? "
                "WHERE id='main' AND phase='waking' AND paused=0",
                (now,),
            ).rowcount
            if changed:
                audit(tx, "sleep.awake", "main", {}, now)

    def pause(self, paused: bool) -> dict:
        with self.db.transaction() as tx:
            tx.execute(
                "UPDATE characters SET paused=?,version=version+1,updated_at=? WHERE id='main'",
                (int(paused), timestamp(self.clock.now())),
            )
            audit(tx, "runtime.pause", "main", {"paused": paused}, timestamp(self.clock.now()))
        if not paused:
            self.tick()
        return self.state()

    def start_work(self, kind: str, activity_id: str | None = None) -> str:
        work_id = str(uuid4())
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            self.check_admission(tx, activity_id)
            tx.execute(
                "INSERT INTO work(id,activity_id,kind,status,started_at) VALUES(?,?,?,'running',?)",
                (work_id, activity_id, kind, now),
            )
            audit(tx, "work.started", work_id, {"kind": kind, "activity_id": activity_id}, now)
        return work_id

    def finish_work(self, work_id: str, outcome: WorkOutcome, *, detail: str = "") -> None:
        if outcome not in ("succeeded", "failed", "cancelled", "unknown"):
            raise ValueError("未知的工作结果")
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            row = tx.execute("SELECT * FROM work WHERE id=?", (work_id,)).fetchone()
            if not row:
                raise NotFound("工作不存在")
            if row["status"] not in ("running", "unknown"):
                if row["status"] == outcome:
                    return
                raise Conflict("已经结束的工作不能改写结果")
            tx.execute(
                "UPDATE work SET status=?,detail=?,finished_at=? WHERE id=?",
                (outcome, detail, now, work_id),
            )
            audit(tx, "work.finished", work_id, {"outcome": outcome, "detail": detail}, now)
            self.try_sleep(tx)

    @asynccontextmanager
    async def work(self, kind: str, activity_id: str | None = None) -> AsyncIterator[str]:
        work_id = self.start_work(kind, activity_id)
        try:
            yield work_id
        except BaseException:
            # 外部工具可能已经执行；交给结果核对而非假定远端停止。
            outcome = "unknown" if kind in ("tool", "delivery") else "failed"
            self.finish_work(work_id, outcome, detail="执行被异常中断")
            raise
        else:
            self.finish_work(work_id, "succeeded")

    def recover(self) -> None:
        """进程重启只核对持久状态，未知副作用不自动重试。"""
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            changed = tx.execute(
                "UPDATE work SET status='unknown',detail='进程中断，等待结果核对' "
                "WHERE status='running'"
            ).rowcount
            tx.execute(
                "UPDATE deliveries SET status='unknown',error='进程中断，需核对回执',updated_at=? "
                "WHERE status='sending'",
                (now,),
            )
            tx.execute(
                "UPDATE activities SET status='interrupted',version=version+1,updated_at=? "
                "WHERE status='running'",
                (now,),
            )
            if changed:
                tx.execute("UPDATE characters SET health='degraded' WHERE id='main'")
            audit(tx, "runtime.recovered", "main", {"unknown_work": changed}, now)
        self.tick()

    def next_wake_at(self) -> datetime | None:
        state = self.state()
        if state["paused"] or state["phase"] != Phase.SLEEPING:
            return None
        return datetime.fromisoformat(state["wake_at"])
