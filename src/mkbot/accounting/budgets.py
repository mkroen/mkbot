"""按调用原子预留多个预算项；未知用量继续占用预留，不算作免费。"""

from dataclasses import dataclass

from mkbot.domain.errors import BudgetExceeded, Conflict, NotFound
from mkbot.runtime.clock import Clock, timestamp
from mkbot.storage.database import Database


@dataclass(frozen=True, slots=True)
class Charge:
    scope: str
    metric: str
    amount: int


class BudgetManager:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db, self.clock = db, clock

    def configure(self, scope: str, metric: str, limit: int) -> None:
        if limit < 0 or not scope or not metric:
            raise ValueError("额度必须非负，且需要明确范围和计量项")
        with self.db.transaction() as tx:
            tx.execute(
                "INSERT INTO budget_limits VALUES(?,?,?) ON CONFLICT(scope,metric) "
                "DO UPDATE SET limit_value=excluded.limit_value",
                (scope, metric, limit),
            )

    def reserve(self, attempt_id: str, charges: list[Charge]) -> None:
        if not attempt_id or not charges:
            raise ValueError("预留必须关联一次调用及计量项")
        keys = {(c.scope, c.metric) for c in charges}
        if len(keys) != len(charges) or any(c.amount < 0 for c in charges):
            raise ValueError("预算项重复或数值为负")
        with self.db.transaction() as tx:
            existing = tx.execute("SELECT * FROM reservations WHERE id=?", (attempt_id,)).fetchall()
            if existing:
                recorded = {(r["scope"], r["metric"], r["reserved"]) for r in existing}
                requested = {(c.scope, c.metric, c.amount) for c in charges}
                if recorded != requested:
                    raise Conflict("同一调用的预算预留不可变更")
                return
            for charge in charges:
                limit = tx.execute(
                    "SELECT limit_value FROM budget_limits WHERE scope=? AND metric=?",
                    (charge.scope, charge.metric),
                ).fetchone()
                if not limit:
                    raise NotFound("尚未配置该范围的预算")
                spent = tx.execute(
                    "SELECT coalesce(sum(CASE WHEN state='settled' THEN actual "
                    "WHEN state='released' THEN 0 ELSE reserved END),0) FROM reservations "
                    "WHERE scope=? AND metric=?",
                    (charge.scope, charge.metric),
                ).fetchone()[0]
                if spent + charge.amount > limit["limit_value"]:
                    raise BudgetExceeded(f"{charge.scope}/{charge.metric} 额度不足")
            tx.executemany(
                "INSERT INTO reservations(id,scope,metric,reserved,created_at) VALUES(?,?,?,?,?)",
                [
                    (attempt_id, c.scope, c.metric, c.amount, timestamp(self.clock.now()))
                    for c in charges
                ],
            )

    def settle(self, attempt_id: str, actual: list[Charge] | None) -> None:
        """actual=None 表示上游没有提供用量；保留保守预留。"""
        if actual is not None and (
            len({(c.scope, c.metric) for c in actual}) != len(actual)
            or any(c.amount < 0 for c in actual)
        ):
            raise ValueError("结算项目重复或数值为负")
        with self.db.transaction() as tx:
            rows = tx.execute("SELECT * FROM reservations WHERE id=?", (attempt_id,)).fetchall()
            if not rows:
                raise NotFound("调用没有预算预留")
            if any(r["state"] == "released" for r in rows):
                raise Conflict("已经释放的未执行调用不能结算")
            amounts = (
                {(c.scope, c.metric): c.amount for c in actual} if actual is not None else None
            )
            if amounts is not None and set(amounts) != {(r["scope"], r["metric"]) for r in rows}:
                raise Conflict("结算必须覆盖原预留的全部计量项")
            for row in rows:
                value = amounts[(row["scope"], row["metric"])] if amounts is not None else None
                if row["state"] == "settled":
                    if value != row["actual"]:
                        raise Conflict("结算结果不能被重复改写")
                    continue
                tx.execute(
                    "UPDATE reservations SET state=?,actual=? WHERE id=? AND scope=? AND metric=?",
                    (
                        "settled" if value is not None else "unknown",
                        value,
                        attempt_id,
                        row["scope"],
                        row["metric"],
                    ),
                )

    def release_unstarted(self, attempt_id: str) -> None:
        with self.db.transaction() as tx:
            rows = tx.execute("SELECT * FROM reservations WHERE id=?", (attempt_id,)).fetchall()
            if not rows or any(r["state"] not in ("reserved", "released") for r in rows):
                raise Conflict("只能释放已证明尚未执行的预留")
            tx.execute("UPDATE reservations SET state='released' WHERE id=?", (attempt_id,))

    def usage(self) -> list[dict]:
        return self.db.rows(
            "SELECT b.scope,b.metric,b.limit_value,"
            "coalesce(sum(CASE WHEN r.state='settled' THEN r.actual ELSE 0 END),0) AS settled,"
            "coalesce(sum(CASE WHEN r.state IN ('reserved','unknown') "
            "THEN r.reserved ELSE 0 END),0) "
            "AS reserved FROM budget_limits b LEFT JOIN reservations r "
            "ON b.scope=r.scope AND b.metric=r.metric GROUP BY b.scope,b.metric"
        )
