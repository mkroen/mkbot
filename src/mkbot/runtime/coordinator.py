"""每个角色只有一个调度所有者；等待事件和持久期限，不轮询模型。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from mkbot.domain.errors import AdmissionDenied, Conflict, ContextInvalidated, NotFound
from mkbot.domain.models import ActivityRequest, ReviewDecision, SleepRequest
from mkbot.runtime.clock import timestamp
from mkbot.runtime.lifecycle import audit
from mkbot.runtime.planning import PlanOutcome
from mkbot.runtime.scheduling import ChatChoice, IdleWait, SchedulerSettings
from mkbot.storage.database import encode

if TYPE_CHECKING:
    from mkbot.runtime.kernel import Kernel


class Coordinator:
    def __init__(self, kernel: Kernel) -> None:
        self.k = kernel
        self.db, self.clock = kernel.db, kernel.clock
        self.busy = False
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            tx.execute(
                "INSERT OR IGNORE INTO "
                "scheduler_state(id,next_idle_at,idle_day,updated_at) "
                "VALUES('main',?,?,?)",
                (
                    timestamp(
                        self.clock.now() + timedelta(seconds=self.settings().idle_interval_seconds)
                    ),
                    self.clock.now().date().isoformat(),
                    now,
                ),
            )
            # 已落库的步骤可以继续；未确认的远端请求留给核对，不重发。
            tx.execute(
                "UPDATE scheduler_jobs SET status=CASE WHEN EXISTS "
                "(SELECT 1 FROM work w WHERE w.activity_id=scheduler_jobs.activity_id "
                "AND w.status='unknown') THEN 'held' ELSE 'ready' END,updated_at=? "
                "WHERE status='running'",
                (now,),
            )

    def settings(self) -> SchedulerSettings:
        return SchedulerSettings.model_validate(self.k.config.get("scheduler")["content"])

    def state(self) -> dict:
        row = self.db.rows("SELECT * FROM scheduler_state WHERE id='main'")[0]
        return row | {
            "busy": self.busy,
            "idle_started_today": row["idle_started"]
            if row["idle_day"] == self.clock.now().date().isoformat()
            else 0,
            "settings": self.settings().model_dump(),
            "jobs": self.jobs(),
            "next_at": self.next_at(),
        }

    def jobs(self) -> list[dict]:
        return self.db.rows(
            "SELECT j.*,a.goal,a.conversation_id,a.status AS activity_status,a.due_at "
            "FROM scheduler_jobs j JOIN activities a ON a.id=j.activity_id "
            "ORDER BY j.created_at DESC,j.rowid DESC LIMIT 100"
        )

    def _reason(self, text: str) -> None:
        with self.db.transaction() as tx:
            tx.execute(
                "UPDATE scheduler_state SET last_reason=?,updated_at=? "
                "WHERE id='main' AND last_reason!=?",
                (text, timestamp(self.clock.now()), text),
            )

    def assert_context(self, activity_id: str) -> None:
        rows = self.db.rows("SELECT * FROM scheduler_jobs WHERE activity_id=?", (activity_id,))
        if not rows or rows[0]["lane"] != "chat" or rows[0]["status"] == "done":
            return
        stale = self.db.rows(
            "SELECT 1 FROM inbox_leases l JOIN inbox i ON i.event_id=l.event_id "
            "WHERE l.activity_id=? AND i.status NOT IN ('pending','deferred') LIMIT 1",
            (activity_id,),
        )
        if stale:
            raise ContextInvalidated("本批留言已由其他入口处置，需要重新检查上下文")

    def decision(self, activity_id: str) -> dict | None:
        rows = self.db.rows(
            "SELECT kind,payload FROM activity_decisions WHERE activity_id=? AND status='pending'",
            (activity_id,),
        )
        return {"kind": rows[0]["kind"], "data": json.loads(rows[0]["payload"])} if rows else None

    def _store_decision(self, activity_id: str, kind: str, payload: dict) -> dict:
        self.k.assert_context(activity_id)
        with self.db.transaction() as tx:
            self.k.lifecycle.check_admission(tx, activity_id)
            existing = tx.execute(
                "SELECT * FROM activity_decisions WHERE activity_id=?", (activity_id,)
            ).fetchone()
            if existing and existing["status"] == "invalidated":
                tx.execute(
                    "UPDATE activity_decisions SET "
                    "kind=?,payload=?,status='pending',created_at=? "
                    "WHERE activity_id=?",
                    (kind, encode(payload), timestamp(self.clock.now()), activity_id),
                )
            elif existing:
                if existing["kind"] != kind or existing["payload"] != encode(payload):
                    raise Conflict("本轮已经选择终止决定，不能再改写为另一种行动")
            else:
                tx.execute(
                    "INSERT INTO activity_decisions(activity_id,kind,payload,created_at) "
                    "VALUES(?,?,?,?)",
                    (activity_id, kind, encode(payload), timestamp(self.clock.now())),
                )
        return {"accepted": True, "end_activity": True}

    def choose_chat(self, activity_id: str, choice: ChatChoice) -> dict:
        jobs = self.db.rows("SELECT lane FROM scheduler_jobs WHERE activity_id=?", (activity_id,))
        if not jobs or jobs[0]["lane"] != "chat":
            raise Conflict("此工具只能处置调度器分配给当前活动的留言")
        if choice.decision == "defer":
            settings = self.settings()
            if choice.revisit_at < self.clock.now() + timedelta(
                seconds=settings.minimum_defer_seconds
            ):
                raise ValueError("再次评估时间过近，请留出配置要求的等待间隔")
            count = self.db.rows(
                "SELECT max(i.review_count) AS n FROM inbox i JOIN inbox_leases l "
                "ON i.event_id=l.event_id WHERE l.activity_id=?",
                (activity_id,),
            )[0]["n"]
            if count is None or count >= settings.max_deferrals:
                raise Conflict("这批留言已达暂缓次数上限，请明确选择是否回复")
        return self._store_decision(activity_id, "chat", choice.model_dump(mode="json"))

    def choose_wait(self, activity_id: str, wait: IdleWait) -> dict:
        self._internal_activity(activity_id)
        if wait.until < self.clock.now() + timedelta(seconds=self.settings().idle_interval_seconds):
            raise ValueError("等待时间必须达到空闲活动的最小间隔")
        return self._store_decision(activity_id, "wait", wait.model_dump(mode="json"))

    def choose_sleep(self, activity_id: str, request: SleepRequest) -> dict:
        self._internal_activity(activity_id)
        if request.earliest_wake_at <= self.clock.now():
            raise ValueError("唤醒时间必须在未来")
        if self.k.lifecycle.state()["phase"] != "awake":
            raise Conflict("只有清醒时可以决定休息")
        return self._store_decision(activity_id, "sleep", request.model_dump(mode="json"))

    def _internal_activity(self, activity_id: str) -> None:
        if self.k.activities.get(activity_id)["conversation_id"]:
            raise Conflict("作息由角色自己的活动决定，不能在他人的聊天里改写")

    def _candidates(self) -> list[dict]:
        """仅用元数据排序；不同人的私聊原文不会进入统一调度 Prompt。"""
        settings, now = self.settings(), self.clock.now()
        rows = self.db.rows(
            "SELECT e.conversation_id,min(e.received_at) AS oldest,max(e.received_at) AS "
            "newest,"
            "max(e.kind='private') AS private,max(json_extract(e.payload,'$.directed')) "
            "AS directed,"
            "max(i.status='deferred') AS deferred,count(*) AS count FROM events e "
            "JOIN inbox i ON i.event_id=e.id LEFT JOIN inbox_leases l ON l.event_id=e.id "
            "WHERE l.event_id IS NULL AND (i.status='pending' OR "
            "(i.status='deferred' AND i.revisit_at<=?)) GROUP BY e.conversation_id",
            (timestamp(now),),
        )
        for row in rows:
            oldest, newest = (
                datetime.fromisoformat(row["oldest"]),
                datetime.fromisoformat(row["newest"]),
            )
            ready = min(
                newest + timedelta(seconds=settings.debounce_seconds),
                oldest + timedelta(seconds=settings.max_batch_wait_seconds),
            )
            if row["deferred"]:
                ready = min(ready, now)
            previous = self.db.rows(
                "SELECT max(j.created_at) AS latest FROM scheduler_jobs j JOIN "
                "activities a "
                "ON a.id=j.activity_id WHERE j.lane='chat' AND a.conversation_id=?",
                (row["conversation_id"],),
            )[0]["latest"]
            if previous:
                ready = max(
                    ready,
                    datetime.fromisoformat(previous)
                    + timedelta(seconds=settings.conversation_cooldown_seconds),
                )
            row["ready_at"] = timestamp(ready)
            age = max(0, (now - oldest).total_seconds())
            # 老请求逐渐提高优先级，普通群不会永远被后到消息挤走。
            row["score"] = (100 if row["directed"] else 80 if row["private"] else 20) + age / 60
        return sorted(rows, key=lambda row: (-row["score"], row["oldest"], row["conversation_id"]))

    def _claim_batch(self, conversation_id: str) -> str:
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            self.k.lifecycle.check_admission(tx)
            ids = [
                row[0]
                for row in tx.execute(
                    "SELECT e.id FROM events e JOIN inbox i ON e.id=i.event_id "
                    "LEFT JOIN inbox_leases l ON l.event_id=e.id WHERE e.conversation_id=? "
                    "AND l.event_id IS NULL AND (i.status='pending' OR "
                    "(i.status='deferred' AND i.revisit_at<=?)) "
                    "ORDER BY e.received_at,e.rowid LIMIT ?",
                    (conversation_id, now, self.settings().max_batch_events),
                )
            ]
            if not ids:
                raise Conflict("当前会话已没有可接取的留言")
            activity_id = self.k.activities.create_in(
                tx,
                ActivityRequest(
                    kind="chat",
                    origin="user",
                    conversation_id=conversation_id,
                    trigger_event_ids=ids,
                    goal="查看本次分配的留言，结合当前会话、人物记忆、时效与后续取消情况，"
                    "选择回复、不回复、暂缓或过期。无需逐句回应；用 life__review 保存明确决定。",
                ),
            )
            tx.executemany(
                "INSERT INTO inbox_leases VALUES(?,?)", [(key, activity_id) for key in ids]
            )
            tx.execute(
                "INSERT INTO scheduler_jobs VALUES(?,'chat','ready',?,?,?)",
                (activity_id, "收到新的会话留言", now, now),
            )
            audit(tx, "scheduler.batch_claimed", activity_id, {"count": len(ids)}, now)
        return activity_id

    def _expire_and_wake(self) -> None:
        now = timestamp(self.clock.now())
        cutoff = timestamp(
            self.clock.now() - timedelta(hours=self.settings().max_message_age_hours)
        )
        with self.db.transaction() as tx:
            expired = tx.execute(
                "UPDATE inbox SET status='reviewed',decision='expired',reason='超过收件保留时效',"
                "reviewed_at=? WHERE status IN ('pending','deferred') AND event_id IN "
                "(SELECT e.id FROM events e LEFT JOIN inbox_leases l ON l.event_id=e.id "
                "WHERE e.received_at<? AND l.event_id IS NULL)",
                (now, cutoff),
            ).rowcount
            if expired:
                audit(tx, "scheduler.messages_expired", "main", {"count": expired}, now)
            phase = tx.execute("SELECT phase FROM characters WHERE id='main'").fetchone()[0]
            state = tx.execute("SELECT wake_cutoff FROM scheduler_state WHERE id='main'").fetchone()
            if phase == "waking" and state[0] is None:
                last = tx.execute("SELECT coalesce(max(rowid),0) FROM events").fetchone()[0]
                tx.execute("UPDATE scheduler_state SET wake_cutoff=? WHERE id='main'", (last,))
            elif phase != "waking" and state[0] is not None:
                tx.execute("UPDATE scheduler_state SET wake_cutoff=NULL WHERE id='main'")
        self._finish_waking()

    def _finish_waking(self) -> None:
        if self.k.lifecycle.state()["phase"] != "waking":
            return
        cutoff = self.db.rows("SELECT wake_cutoff FROM scheduler_state WHERE id='main'")[0][
            "wake_cutoff"
        ]
        if cutoff is None:
            return
        pending = self.db.rows(
            "SELECT 1 FROM events e JOIN inbox i ON i.event_id=e.id "
            "LEFT JOIN inbox_leases l ON l.event_id=e.id "
            "LEFT JOIN scheduler_jobs j ON j.activity_id=l.activity_id WHERE e.rowid<=? "
            "AND (i.status='pending' OR (i.status='deferred' AND i.revisit_at<=?)) "
            "AND (j.status IS NULL OR j.status NOT IN ('held','done')) LIMIT 1",
            (cutoff, timestamp(self.clock.now())),
        )
        if not pending:
            self.k.lifecycle.finish_waking()
            with self.db.transaction() as tx:
                tx.execute("UPDATE scheduler_state SET wake_cutoff=NULL WHERE id='main'")

    def finish(self, activity_id: str) -> None:
        """终止决定在活动完成后落地；崩溃后可按同一活动幂等补齐。"""
        activity = self.k.activities.get(activity_id)
        if activity["status"] != "completed":
            return
        decision = self.decision(activity_id)
        jobs = self.db.rows("SELECT * FROM scheduler_jobs WHERE activity_id=?", (activity_id,))
        if not jobs and not decision:
            return
        if jobs and jobs[0]["status"] == "done":
            return
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            plan_valid = self.k.plans.finish_in(
                tx,
                activity_id,
                PlanOutcome.model_validate(decision["data"])
                if decision and decision["kind"] == "plan"
                else None,
            )
            if jobs and jobs[0]["lane"] == "chat":
                ids = [
                    row[0]
                    for row in tx.execute(
                        "SELECT event_id FROM inbox_leases WHERE activity_id=?", (activity_id,)
                    )
                ]
                if ids:
                    result = activity["checkpoint"].get("agent", {}).get("result") or {}
                    choice = (
                        ChatChoice.model_validate(decision["data"])
                        if decision
                        else ChatChoice(
                            decision="reply" if result.get("delivery_id") else "no_reply",
                            text=result.get("text", "") if result.get("delivery_id") else "",
                            reason="主会话给出最终回复"
                            if result.get("delivery_id")
                            else "主会话选择不回复",
                        )
                    )
                    self.k.inbox.resolve_in(
                        tx,
                        ReviewDecision(
                            event_ids=ids,
                            decision=choice.decision,
                            reason=choice.reason,
                            revisit_at=choice.revisit_at,
                        ),
                    )
                    tx.execute("DELETE FROM inbox_leases WHERE activity_id=?", (activity_id,))
            if decision and decision["kind"] == "wait":
                until = IdleWait.model_validate(decision["data"]).until
                tx.execute(
                    "UPDATE scheduler_state SET next_idle_at=max(next_idle_at,?) WHERE id='main'",
                    (timestamp(until),),
                )
            elif not decision or decision["kind"] != "sleep":
                tx.execute(
                    "UPDATE scheduler_state SET next_idle_at=max(next_idle_at,?) WHERE id='main'",
                    (
                        timestamp(
                            self.clock.now()
                            + timedelta(seconds=self.settings().idle_interval_seconds)
                        ),
                    ),
                )
            decision_status = "invalidated" if plan_valid is False else "applied"
            if decision and decision["kind"] == "sleep":
                request = SleepRequest.model_validate(decision["data"])
                state = tx.execute("SELECT * FROM characters WHERE id='main'").fetchone()
                if (
                    state["phase"] == "awake"
                    and not state["paused"]
                    and request.earliest_wake_at > self.clock.now()
                ):
                    self.k.lifecycle.request_sleep_in(tx, request)
                else:
                    decision_status = "invalidated"
                    audit(tx, "scheduler.sleep_obsolete", activity_id, {}, now)
            if decision:
                tx.execute(
                    "UPDATE activity_decisions SET status=? WHERE activity_id=?",
                    (decision_status, activity_id),
                )
            tx.execute(
                "UPDATE scheduler_jobs SET status='done',updated_at=? WHERE activity_id=?",
                (now, activity_id),
            )
        self._finish_waking()

    def _reconcile(self) -> None:
        self.k.plans.reconcile()
        for row in self.db.rows(
            "SELECT j.*,a.status AS activity_status FROM scheduler_jobs j "
            "JOIN activities a ON a.id=j.activity_id WHERE j.status!='done'"
        ):
            if row["activity_status"] == "completed":
                self.finish(row["activity_id"])
            elif row["activity_status"] in ("cancelled", "failed"):
                with self.db.transaction() as tx:
                    ids = [
                        r[0]
                        for r in tx.execute(
                            "SELECT l.event_id FROM inbox_leases l JOIN inbox i "
                            "ON i.event_id=l.event_id WHERE l.activity_id=? "
                            "AND i.status IN ('pending','deferred')",
                            (row["activity_id"],),
                        )
                    ]
                    if ids:
                        self.k.inbox.resolve_in(
                            tx,
                            ReviewDecision(
                                event_ids=ids,
                                decision="no_reply",
                                reason="关联活动已取消或终止",
                            ),
                        )
                    tx.execute(
                        "DELETE FROM inbox_leases WHERE activity_id=?", (row["activity_id"],)
                    )
                    tx.execute(
                        "UPDATE scheduler_jobs SET status='done',updated_at=? WHERE activity_id=?",
                        (timestamp(self.clock.now()), row["activity_id"]),
                    )

    def _scheduled_jobs(self) -> list[dict]:
        settings = self.settings()
        waking = self.k.lifecycle.state()["phase"] == "waking"
        rows = self.db.rows(
            "SELECT j.*,a.due_at,a.priority FROM scheduler_jobs j "
            "JOIN activities a ON a.id=j.activity_id "
            "WHERE j.status IN ('ready','yielded','waiting') "
            "AND a.status IN ('queued','paused','waiting','interrupted') AND NOT EXISTS "
            "(SELECT 1 FROM work w WHERE w.activity_id=a.id AND w.status IN "
            "('running','unknown')) "
            "ORDER BY a.priority DESC,j.created_at,j.rowid"
        )
        return [
            row
            for row in rows
            if (row["lane"] == "chat" and settings.chat_enabled)
            or (
                not waking
                and (
                    (row["lane"] == "plan" and settings.run_plans)
                    or (row["lane"] == "idle" and settings.autonomous_enabled)
                )
            )
        ]

    def _ready_jobs(self) -> list[dict]:
        now = timestamp(self.clock.now())
        return [row for row in self._scheduled_jobs() if not row["due_at"] or row["due_at"] <= now]

    def _adopt_plans(self) -> None:
        if not self.settings().run_plans or self.k.lifecycle.state()["phase"] != "awake":
            return
        self.k.plans.dispatch_due()
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            tx.execute(
                "INSERT OR IGNORE INTO scheduler_jobs SELECT "
                "a.id,'plan','ready','已有计划到期',?,? "
                "FROM activities a WHERE a.status='queued' AND a.kind!='chat' "
                "AND (a.due_at IS NULL OR a.due_at<=?)",
                (now, now, now),
            )

    def _can_idle(self) -> bool:
        settings = self.settings()
        if not settings.autonomous_enabled or not settings.idle_max_runs_per_day:
            return False
        if self.k.lifecycle.state()["phase"] != "awake":
            return False
        return not self.db.rows(
            "SELECT 1 FROM scheduler_jobs WHERE status IN "
            "('held','waiting','ready','yielded','running') "
            "UNION ALL SELECT 1 FROM work WHERE status IN ('running','unknown') "
            "UNION ALL SELECT 1 FROM activities WHERE status='running' "
            "UNION ALL SELECT 1 FROM deliveries WHERE status IN "
            "('pending','sending','unknown') LIMIT 1"
        )

    def _start_idle(self) -> str | None:
        if not self._can_idle():
            return None
        now, settings = timestamp(self.clock.now()), self.settings()
        with self.db.transaction() as tx:
            state = tx.execute("SELECT * FROM scheduler_state WHERE id='main'").fetchone()
            day = self.clock.now().date().isoformat()
            used = state["idle_started"] if state["idle_day"] == day else 0
            if state["next_idle_at"] > now or used >= settings.idle_max_runs_per_day:
                return None
            self.k.lifecycle.check_admission(tx)
            activity_id = self.k.activities.create_in(
                tx,
                ActivityRequest(
                    kind="idle",
                    goal=settings.idle_goal,
                    origin="autonomous",
                ),
            )
            tx.execute(
                "INSERT INTO scheduler_jobs VALUES(?,'idle','ready','清醒时的空闲机会',?,?)",
                (activity_id, now, now),
            )
            tx.execute(
                "UPDATE scheduler_state SET "
                "idle_day=?,idle_started=?,next_idle_at=?,updated_at=? WHERE id='main'",
                (
                    day,
                    used + 1,
                    timestamp(self.clock.now() + timedelta(seconds=settings.idle_interval_seconds)),
                    now,
                ),
            )
        return activity_id

    def next_at(self) -> str | None:
        phase = self.k.lifecycle.state()
        if phase["paused"]:
            return None
        if phase["phase"] == "sleeping":
            return phase["wake_at"]
        settings = self.settings()
        if not settings.enabled or phase["phase"] == "winding_down":
            return None
        now = self.clock.now()
        due = []
        candidates = self._candidates() if settings.chat_enabled else []
        if settings.chat_enabled:
            due.extend(row["ready_at"] for row in candidates)
            deferred = self.db.rows(
                "SELECT min(i.revisit_at) AS due FROM inbox i LEFT JOIN inbox_leases l "
                "ON l.event_id=i.event_id "
                "WHERE i.status='deferred' AND l.event_id IS NULL AND i.revisit_at>?",
                (timestamp(now),),
            )[0]["due"]
            if deferred:
                due.append(deferred)
        for row in self._scheduled_jobs():
            if row["lane"] != "chat" and candidates:
                continue
            due.append(row["due_at"] or timestamp(now))
        if settings.run_plans and phase["phase"] == "awake" and not candidates:
            plan_due = self.k.plans.next_at()
            if plan_due:
                due.append(plan_due)
            future = self.db.rows(
                "SELECT min(coalesce(a.due_at,?)) AS due FROM activities a "
                "LEFT JOIN scheduler_jobs j ON j.activity_id=a.id "
                "WHERE a.status='queued' AND a.kind!='chat' AND j.activity_id IS NULL",
                (timestamp(now),),
            )[0]["due"]
            if future:
                due.append(future)
        state = self.db.rows("SELECT * FROM scheduler_state WHERE id='main'")[0]
        if not candidates and self._can_idle():
            next_idle = state["next_idle_at"]
            if (
                state["idle_day"] == now.date().isoformat()
                and state["idle_started"] >= settings.idle_max_runs_per_day
            ):
                next_idle = max(
                    next_idle,
                    timestamp(
                        (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                    ),
                )
            due.append(next_idle)
        if not due:
            return None
        selected = min(due)
        if state["next_dispatch_at"]:
            selected = max(selected, state["next_dispatch_at"])
        return selected

    def resume(self, activity_id: str) -> dict:
        self.k.assert_context(activity_id)
        with self.db.transaction() as tx:
            row = tx.execute(
                "SELECT * FROM scheduler_jobs WHERE activity_id=?", (activity_id,)
            ).fetchone()
            if not row:
                raise NotFound("调度活动不存在")
            if row["status"] != "held":
                raise Conflict("仅需要人工处理的活动可以重新接回调度")
            if tx.execute(
                "SELECT 1 FROM work WHERE activity_id=? AND status IN ('running','unknown')",
                (activity_id,),
            ).fetchone():
                raise AdmissionDenied("请先核对未完成或未知的工作")
            tx.execute(
                "UPDATE scheduler_jobs SET status='ready',reason='操作者请求继续',updated_at=? "
                "WHERE activity_id=?",
                (timestamp(self.clock.now()), activity_id),
            )
        self.k.notify()
        return {"activity_id": activity_id, "status": "ready"}

    async def step(self) -> dict:
        if self.busy or self.k.agent.busy:
            return {"status": "busy"}
        self.busy = True
        try:
            return await self._step()
        finally:
            self.busy = False

    async def _step(self) -> dict:
        self.k.lifecycle.tick()
        phase, settings = self.k.lifecycle.state(), self.settings()
        if (
            phase["paused"]
            or phase["phase"] in ("sleeping", "winding_down")
            or not settings.enabled
        ):
            reason = (
                "运行已暂停"
                if phase["paused"]
                else "自动调度未启用"
                if not settings.enabled
                else "等待作息或当前工作收尾"
            )
            self._reason(reason)
            return {"status": "waiting", "reason": reason, "next_at": self.next_at()}
        self._reconcile()
        if self.k.lifecycle.state()["phase"] in ("sleeping", "winding_down"):
            return {"status": "waiting", "next_at": self.next_at()}
        if settings.chat_enabled:
            self._expire_and_wake()
        else:
            self.k.lifecycle.finish_waking()
        state = self.db.rows("SELECT * FROM scheduler_state WHERE id='main'")[0]
        now = timestamp(self.clock.now())
        if state["next_dispatch_at"] and state["next_dispatch_at"] > now:
            return {"status": "waiting", "next_at": self.next_at()}
        self._adopt_plans()
        jobs = self._ready_jobs()
        chats = [row for row in jobs if row["lane"] == "chat"]
        background = [row for row in jobs if row["lane"] != "chat"]
        pending = self._candidates() if settings.chat_enabled else []
        candidates = [row for row in pending if row["ready_at"] <= now]
        fairness = bool(
            background and (chats or candidates) and state["chat_burst"] >= settings.max_chat_burst
        )
        if fairness:
            selected = background[0]["activity_id"]
        elif chats:
            selected = chats[0]["activity_id"]
        elif candidates:
            selected = self._claim_batch(candidates[0]["conversation_id"])
        elif pending:
            selected = None
        elif background:
            selected = background[0]["activity_id"]
        else:
            selected = self._start_idle()
        if not selected:
            self._reason("等待新留言、到期计划或下一次空闲机会")
            return {"status": "waiting", "next_at": self.next_at()}
        job = self.db.rows("SELECT * FROM scheduler_jobs WHERE activity_id=?", (selected,))[0]
        with self.db.transaction() as tx:
            tx.execute(
                "UPDATE scheduler_jobs SET status='running',updated_at=? WHERE activity_id=?",
                (now, selected),
            )
            tx.execute(
                "UPDATE scheduler_state SET "
                "chat_burst=?,next_dispatch_at=?,last_reason=?,updated_at=? WHERE "
                "id='main'",
                (
                    state["chat_burst"] + 1 if job["lane"] == "chat" else 0,
                    timestamp(
                        self.clock.now() + timedelta(seconds=settings.dispatch_interval_seconds)
                    ),
                    "正在处理留言" if job["lane"] == "chat" else "正在继续自己的活动",
                    now,
                ),
            )
            audit(tx, "scheduler.dispatched", selected, {"lane": job["lane"]}, now)
        boundaries = 0

        def should_yield() -> str | None:
            nonlocal boundaries
            boundaries += 1
            if self.k._stopped:
                return "shutdown"
            if self.k.lifecycle.state()["paused"]:
                return "operator_pause"
            if not self.settings().enabled:
                return "disabled"
            if job["lane"] != "chat" and self.settings().chat_enabled:
                if (not fairness or boundaries > 1) and self._candidates():
                    return "new_messages"
            return None

        try:
            result = await self.k.agent.run(selected, yield_check=should_yield)
            if result["status"] == "completed":
                self.finish(selected)
            else:
                status = (
                    "yielded"
                    if result.get("yielded")
                    else ("waiting" if result.get("retry_at") else "held")
                )
                with self.db.transaction() as tx:
                    tx.execute(
                        "UPDATE scheduler_jobs SET status=?,reason=?,updated_at=? WHERE "
                        "activity_id=?",
                        (
                            status,
                            result.get("reason", "等待继续"),
                            timestamp(self.clock.now()),
                            selected,
                        ),
                    )
        except Exception:
            with self.db.transaction() as tx:
                tx.execute(
                    "UPDATE scheduler_jobs SET "
                    "status='held',reason='运行中断，等待核对',updated_at=? WHERE activity_id=?",
                    (timestamp(self.clock.now()), selected),
                )
                audit(
                    tx,
                    "scheduler.error",
                    selected,
                    {"reason": "execution_failed"},
                    timestamp(self.clock.now()),
                )
            result = {"status": "held", "reason": "运行中断，等待核对"}
        self._finish_waking()
        self.k.notify()
        return {"status": "dispatched", "activity_id": selected, "result": result}

    async def run(self) -> None:
        while not self.k._stopped:
            self.k._changed.clear()
            result = await self.step()
            if self.k._stopped:
                break
            due = None if result["status"] == "busy" else self.next_at()
            timeout = (
                max(0.05, (datetime.fromisoformat(due) - self.clock.now()).total_seconds())
                if due
                else None
            )
            try:
                await asyncio.wait_for(self.k._changed.wait(), timeout=timeout)
            except TimeoutError:
                pass
