from concurrent.futures import ThreadPoolExecutor

import pytest

from mkbot.accounting.budgets import BudgetManager, Charge
from mkbot.domain.errors import BudgetExceeded, Conflict, UnsupportedSchema
from mkbot.storage.database import Database


def test_multi_scope_reservation_is_atomic(kernel):
    budget = kernel.budgets
    budget.configure("global", "tokens", 10)
    budget.configure("run", "tokens", 3)
    with pytest.raises(BudgetExceeded):
        budget.reserve("attempt", [Charge("global", "tokens", 4), Charge("run", "tokens", 4)])
    assert all(r["reserved"] == 0 for r in budget.usage())


def test_concurrent_reservations_cannot_spend_same_balance(kernel, clock):
    kernel.budgets.configure("daily", "tokens", 10)
    second_db = Database(kernel.db.path)
    second = BudgetManager(second_db, clock)

    def reserve(manager, attempt):
        try:
            manager.reserve(attempt, [Charge("daily", "tokens", 7)])
            return True
        except BudgetExceeded:
            return False

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(reserve, kernel.budgets, "first"),
                pool.submit(reserve, second, "second"),
            ]
            assert sorted(f.result() for f in futures) == [False, True]
    finally:
        second_db.close()
    assert kernel.budgets.usage()[0]["reserved"] == 7


def test_unknown_usage_retains_reservation_and_settlement_is_idempotent(kernel):
    budget = kernel.budgets
    budget.configure("daily", "microcurrency", 10)
    charges = [Charge("daily", "microcurrency", 8)]
    budget.reserve("one", charges)
    budget.reserve("one", charges)
    budget.settle("one", None)
    with pytest.raises(Conflict):
        budget.release_unstarted("one")
    with pytest.raises(BudgetExceeded):
        budget.reserve("two", [Charge("daily", "microcurrency", 3)])
    budget.settle("one", [Charge("daily", "microcurrency", 6)])
    budget.settle("one", [Charge("daily", "microcurrency", 6)])
    assert budget.usage()[0]["settled"] == 6
    assert budget.usage()[0]["reserved"] == 0
    with pytest.raises(Conflict):
        budget.settle("one", [Charge("daily", "microcurrency", 1)])


def test_config_conflict_and_revisions(kernel):
    first = kernel.config.get("persona")
    updated = kernel.config.put("persona", first["content"] | {"style": "温柔"}, first["revision"])
    with pytest.raises(Conflict):
        kernel.config.put("persona", {"style": "过期覆盖"}, first["revision"])
    assert kernel.config.get("persona")["content"]["style"] == "温柔"
    restored = kernel.config.put("persona", first["content"], updated["revision"])
    assert restored["revision"] == 3
    assert len(kernel.config.history("persona")) == 3


def test_future_database_version_is_not_overwritten(tmp_path):
    db = Database(tmp_path / "future.sqlite3")
    db.connection.execute("PRAGMA user_version=999")
    db.close()
    with pytest.raises(UnsupportedSchema):
        Database(tmp_path / "future.sqlite3")
