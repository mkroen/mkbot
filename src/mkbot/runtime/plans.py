"""计划元数据、到期执行与收尾。每轮执行继承原活动根，不刷新预算。"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from mkbot.domain.errors import AdmissionDenied, Conflict, ContextInvalidated, NotFound
from mkbot.domain.models import ActivityRequest
from mkbot.runtime.clock import timestamp
from mkbot.runtime.lifecycle import audit
from mkbot.runtime.planning import ManualPlan, PlanEdit, PlanOutcome, PlanProposal
from mkbot.runtime.scheduling import SchedulerSettings

if TYPE_CHECKING:
    from mkbot.runtime.kernel import Kernel


class PlanService:
    def __init__(self, kernel: Kernel) -> None:
        self.k, self.db, self.clock = kernel, kernel.db, kernel.clock

    def settings(self) -> SchedulerSettings:
        return SchedulerSettings.model_validate(self.k.config.get("scheduler")["content"])

    def get(self, plan_id: str) -> dict:
        rows = self.db.rows("SELECT * FROM activity_plans WHERE activity_id=?", (plan_id,))
        if not rows:
            raise NotFound("目标不存在")
        runs = self.db.rows(
            "SELECT a.id,a.status,a.due_at,a.created_at,j.status AS scheduler_status,j.reason "
            "FROM plan_runs r JOIN activities a ON a.id=r.activity_id "
            "LEFT JOIN scheduler_jobs j ON j.activity_id=a.id WHERE r.plan_id=? "
            "ORDER BY a.rowid DESC",
            (plan_id,),
        )
        return self.k.activities.get(plan_id) | rows[0] | {"runs": runs}

    def list(self) -> list[dict]:
        return [
            self.get(row["activity_id"])
            for row in self.db.rows(
                "SELECT p.activity_id FROM activity_plans p "
                "JOIN activities a ON a.id=p.activity_id "
                "ORDER BY a.updated_at DESC,a.rowid DESC LIMIT 200"
            )
        ]

    def _validate_due(self, due_at, *, model: bool) -> None:
        if due_at is None:
            return
        minimum = self.clock.now() + timedelta(
            seconds=self.settings().minimum_plan_interval_seconds if model else 0
        )
        if due_at < minimum:
            raise Conflict("下一步执行时间过早，请保留计划间隔")

    def create(
        self, request: PlanProposal, *, activity_id: str | None = None, key: str | None = None
    ) -> dict:
        settings = self.settings()
        if activity_id:
            self.k.assert_context(activity_id)
            if not key or len(key) > 200:
                raise ValueError("创建目标需要稳定的工具调用键")
        with self.db.transaction() as tx:
            if activity_id:
                self.k.lifecycle.check_admission(tx, activity_id)
                found = tx.execute(
                    "SELECT activity_id FROM activity_plans WHERE creator_id=? AND creation_key=?",
                    (activity_id, key),
                ).fetchone()
                if found:
                    return self.get(found[0])
            self._validate_due(request.due_at, model=bool(activity_id))
            count = tx.execute(
                "SELECT count(*) FROM activity_plans p JOIN activities a ON a.id=p.activity_id "
                "WHERE a.status NOT IN ('completed','cancelled','failed')"
            ).fetchone()[0]
            if count >= settings.max_open_plans:
                raise AdmissionDenied("未结束目标数量已到上限，请先收尾已有目标")
            conversation = request.conversation_id if isinstance(request, ManualPlan) else None
            if activity_id:
                parent = tx.execute(
                    "SELECT * FROM activities WHERE id=?", (activity_id,)
                ).fetchone()
                conversation = parent["conversation_id"]
                count = tx.execute(
                    "SELECT count(*) FROM activity_plans p JOIN activities a ON a.id=p.activity_id "
                    "WHERE a.root_id=?",
                    (parent["root_id"],),
                ).fetchone()[0]
                if count >= settings.max_plans_per_root:
                    raise AdmissionDenied("此根活动创建的目标已到上限，取消目标不会恢复此额度")
            plan_id = self.k.activities.create_in(
                tx,
                ActivityRequest(
                    kind="long_term_goal",
                    goal=request.goal,
                    origin="autonomous" if activity_id else "operator",
                    conversation_id=conversation,
                    parent_id=activity_id,
                    due_at=request.due_at,
                ),
            )
            tx.execute("UPDATE activities SET status='waiting' WHERE id=?", (plan_id,))
            tx.execute(
                "INSERT INTO activity_plans(activity_id,next_step,creator_id,creation_key) "
                "VALUES(?,?,?,?)",
                (plan_id, request.next_step, activity_id, key),
            )
            if activity_id:
                self.k.memory.inherit_context_in(tx, activity_id, plan_id)
            audit(
                tx,
                "plan.created",
                plan_id,
                {"creator_id": activity_id},
                timestamp(self.clock.now()),
            )
        self.k.notify()
        return self.get(plan_id)

    def edit(self, plan_id: str, request: PlanEdit) -> dict:
        self.reconcile()
        self._validate_due(request.due_at, model=False)
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            item = self.get(plan_id)
            if item["version"] != request.expected_version:
                raise Conflict("目标版本已变化，请刷新后编辑")
            if item["status"] in ("completed", "cancelled", "failed"):
                raise Conflict("已结束目标不能重新开启")
            if tx.execute(
                "SELECT 1 FROM plan_runs WHERE plan_id=? AND applied=0", (plan_id,)
            ).fetchone():
                raise Conflict("该目标还有未收尾的执行，请先完成或核对该轮活动")
            if tx.execute(
                "SELECT 1 FROM context_invalidations WHERE activity_id=?", (plan_id,)
            ).fetchone():
                if request.status != "cancelled":
                    raise Conflict("目标依据已失效，只能取消；请根据有效资料重新建立目标")
                # 取消不能把已清除的私人资料写回来。
                request = request.model_copy(
                    update={"goal": item["goal"], "next_step": item["next_step"], "progress": ""}
                )
            tx.execute(
                "UPDATE activities SET goal=?,status=?,due_at=?,version=version+1,updated_at=? "
                "WHERE id=?",
                (
                    request.goal,
                    request.status,
                    timestamp(request.due_at) if request.due_at else None,
                    now,
                    plan_id,
                ),
            )
            tx.execute(
                "UPDATE activity_plans SET next_step=?,progress=? WHERE activity_id=?",
                (request.next_step, request.progress, plan_id),
            )
            audit(tx, "plan.edited", plan_id, {"status": request.status}, now)
        self.k.notify()
        return self.get(plan_id)

    def context(self, activity_id: str) -> list[dict]:
        """自己的目标进入自己的空闲上下文；私聊计划仅在原会话中出现。"""
        with self.db.transaction() as tx:
            self.k.lifecycle.check_admission(tx, activity_id)
            audience = tx.execute(
                "SELECT conversation_id FROM activities WHERE id=?", (activity_id,)
            ).fetchone()[0]
            rows = tx.execute(
                "SELECT a.id,a.goal,a.status,a.due_at,p.next_step,p.progress,p.last_result "
                "FROM activity_plans p JOIN activities a ON a.id=p.activity_id "
                "WHERE a.conversation_id IS ? AND a.status IN ('waiting','paused') AND "
                "NOT EXISTS (SELECT 1 FROM context_invalidations c WHERE c.activity_id=a.id) "
                "ORDER BY a.id IN (SELECT plan_id FROM plan_runs WHERE activity_id=?) DESC, "
                "a.updated_at DESC,a.rowid DESC LIMIT 5",
                (audience, activity_id),
            ).fetchall()
            result = []
            for row in rows:
                try:
                    self.k.memory.inherit_context_in(tx, row["id"], activity_id)
                except ContextInvalidated:
                    continue
                result.append(dict(row))
            return result

    def next_at(self) -> str | None:
        return self.db.rows(
            "SELECT min(a.due_at) AS due FROM activity_plans p "
            "JOIN activities a ON a.id=p.activity_id "
            "WHERE a.status='waiting' AND NOT EXISTS "
            "(SELECT 1 FROM plan_runs r WHERE r.plan_id=a.id AND r.applied=0)"
        )[0]["due"]

    def dispatch_due(self) -> None:
        now = timestamp(self.clock.now())
        settings = self.settings()
        with self.db.transaction() as tx:
            self.k.lifecycle.check_admission(tx)
            if (
                self.k.lifecycle.state()["phase"] != "awake"
                or not settings.enabled
                or not settings.run_plans
            ):
                return
            rows = tx.execute(
                "SELECT a.*,p.next_step FROM activity_plans p "
                "JOIN activities a ON a.id=p.activity_id "
                "WHERE a.status='waiting' AND a.due_at<=? AND NOT EXISTS "
                "(SELECT 1 FROM plan_runs r WHERE r.plan_id=a.id AND r.applied=0)",
                (now,),
            ).fetchall()
            for row in rows:
                reason = None
                if tx.execute(
                    "SELECT 1 FROM work w JOIN activities a ON a.id=w.activity_id "
                    "WHERE a.root_id=? AND w.status IN ('running','unknown')",
                    (row["root_id"],),
                ).fetchone():
                    reason = "同一根活动尚有在途或未知结果，请先核对后重新安排"
                try:
                    self.k.memory._assert_context(tx, row["id"])
                except ContextInvalidated:
                    reason = "目标依据已变化，等待核对"
                runs = tx.execute(
                    "SELECT count(*) FROM plan_runs WHERE plan_id=?", (row["id"],)
                ).fetchone()[0]
                if runs >= settings.max_runs_per_plan:
                    reason = "此目标的执行轮数已到上限"
                if reason:
                    tx.execute(
                        "UPDATE activities SET status='paused',due_at=NULL,version=version+1,"
                        "updated_at=? WHERE id=?",
                        (now, row["id"]),
                    )
                    tx.execute(
                        "UPDATE activity_plans SET last_result=? WHERE activity_id=?",
                        (reason, row["id"]),
                    )
                    audit(tx, "plan.held", row["id"], {"reason": reason}, now)
                    continue
                child = self.k.activities.create_in(
                    tx,
                    ActivityRequest(
                        kind="plan_step",
                        goal="推进已有目标：" + row["goal"],
                        origin="autonomous",
                        conversation_id=row["conversation_id"],
                        parent_id=row["id"],
                    ),
                )
                self.k.memory.inherit_context_in(tx, row["id"], child)
                tx.execute(
                    "INSERT INTO plan_runs(activity_id,plan_id) VALUES(?,?)", (child, row["id"])
                )
                tx.execute(
                    "UPDATE activities SET due_at=NULL,version=version+1,updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
                audit(
                    tx, "plan.dispatched", row["id"], {"activity_id": child, "run": runs + 1}, now
                )

    def choose(self, activity_id: str, outcome: PlanOutcome) -> dict:
        if not self.db.rows(
            "SELECT 1 FROM plan_runs WHERE activity_id=? AND applied=0", (activity_id,)
        ):
            raise Conflict("只能在当前目标的执行活动中保存进度")
        self._validate_due(outcome.due_at, model=True)
        return self.k.scheduler._store_decision(
            activity_id, "plan", outcome.model_dump(mode="json")
        )

    def finish_in(self, tx, activity_id: str, outcome: PlanOutcome | None) -> bool | None:
        row = tx.execute(
            "SELECT * FROM plan_runs WHERE activity_id=? AND applied=0", (activity_id,)
        ).fetchone()
        if not row:
            return
        plan_id, now = row["plan_id"], timestamp(self.clock.now())
        valid = True
        if outcome:
            try:
                self.k.memory.inherit_context_in(tx, activity_id, plan_id)
            except ContextInvalidated:
                self.k.memory._invalidate_activities(
                    tx, {activity_id, plan_id}, "目标收尾时发现依据已失效"
                )
                outcome, valid = None, False
        if outcome:
            previous = tx.execute(
                "SELECT * FROM activity_plans WHERE activity_id=?", (plan_id,)
            ).fetchone()
            status = {"continue": "waiting", "pause": "paused"}.get(
                outcome.decision, outcome.decision
            )
            due = timestamp(outcome.due_at) if outcome.due_at else None
            if due and due <= now:
                status, due = "paused", None
            unchanged = (
                outcome.decision == "continue"
                and outcome.progress.strip() == previous["progress"].strip()
                and outcome.next_step.strip() == previous["next_step"].strip()
            )
            result = outcome.result
            if unchanged:
                status, due = "paused", None
                result += "\n进度和下一步均未变化，已停止自动重复安排。"
            tx.execute(
                "UPDATE activity_plans SET next_step=?,progress=?,last_result=? "
                "WHERE activity_id=?",
                (outcome.next_step, outcome.progress, result, plan_id),
            )
        else:
            status, due = "paused", None
            tx.execute(
                "UPDATE activity_plans SET last_result=? WHERE activity_id=?",
                ("本轮已结束但未提交有效的下一步，等待安排", plan_id),
            )
        tx.execute(
            "UPDATE activities SET status=?,due_at=?,version=version+1,updated_at=? WHERE id=?",
            (status, due, now, plan_id),
        )
        tx.execute("UPDATE plan_runs SET applied=1 WHERE activity_id=?", (activity_id,))
        audit(tx, "plan.progressed", plan_id, {"activity_id": activity_id, "status": status}, now)
        return valid

    def reconcile(self) -> None:
        with self.db.transaction() as tx:
            for row in tx.execute(
                "SELECT r.activity_id FROM plan_runs r JOIN activities a ON a.id=r.activity_id "
                "WHERE r.applied=0 AND a.status IN ('cancelled','failed')"
            ).fetchall():
                self.finish_in(tx, row[0], None)
