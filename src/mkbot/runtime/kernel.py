"""运行容器统一组装服务，所有入口共享同一个生命周期与数据库。"""

import asyncio
import json
from pathlib import Path

from mkbot.accounting.budgets import BudgetManager
from mkbot.actions.outbox import Outbox
from mkbot.domain.errors import NotFound
from mkbot.models.contracts import GatewaySettings
from mkbot.models.gateway import ModelGateway
from mkbot.plugins.notebook import notebook_plugin
from mkbot.plugins.registry import PluginRegistry
from mkbot.runtime.activities import ActivityService
from mkbot.runtime.agent import AgentLoop, ExecutionSettings
from mkbot.runtime.clock import Clock, SystemClock
from mkbot.runtime.inbox import InboxService
from mkbot.runtime.lifecycle import Lifecycle
from mkbot.storage.config import ConfigStore
from mkbot.storage.database import Database
from mkbot.storage.instance_lock import InstanceLock


class Kernel:
    def __init__(self, data_dir: Path, clock: Clock | None = None) -> None:
        self.clock = clock or SystemClock()
        self.instance_lock = InstanceLock(data_dir / "instance.lock")
        self.instance_lock.acquire()
        try:
            self.db = Database(data_dir / "mkbot.sqlite3")
            self.lifecycle = Lifecycle(self.db, self.clock)
            self.activities = ActivityService(self.lifecycle)
            self.inbox = InboxService(self.lifecycle)
            self.outbox = Outbox(self.lifecycle)
            self.budgets = BudgetManager(self.db, self.clock)
            self.config = ConfigStore(self.db, self.clock)
            try:
                self.config.get("persona")
            except NotFound:
                self.config.put(
                    "persona",
                    {
                        "name": "mkbot",
                        "identity": "你是一个拥有持续经历、兴趣和关系的角色。",
                        "style": "整体温柔、自然，偶尔俏皮。",
                        "interests": [],
                    },
                    expected_revision=0,
                )
            try:
                self.config.get("models")
            except NotFound:
                self.config.put("models", GatewaySettings().model_dump(mode="json"), 0)
            try:
                self.config.get("execution")
            except NotFound:
                self.config.put("execution", ExecutionSettings().model_dump(mode="json"), 0)
            self.lifecycle.recover()
            self.models = ModelGateway(self.lifecycle, self.config, self.budgets)
            self.plugins = PluginRegistry(self.lifecycle, self.models)
            self.plugins.register(notebook_plugin())
            self.agent = AgentLoop(self)
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self.instance_lock.release()
            raise
        self._changed = asyncio.Event()
        self._stopped = False

    def notify(self) -> None:
        self._changed.set()

    async def run_timers(self) -> None:
        """只在状态改变或唤醒时间到达时工作，没有模型轮询。"""
        while not self._stopped:
            self._changed.clear()
            self.lifecycle.tick()
            due = self.lifecycle.next_wake_at()
            timeout = max(0.001, (due - self.clock.now()).total_seconds()) if due else None
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=timeout)
            except TimeoutError:
                continue

    def stop(self) -> None:
        self._stopped = True
        self.notify()

    def close(self) -> None:
        self.db.close()
        self.instance_lock.release()

    def audit(self, limit: int = 100) -> list[dict]:
        return [
            row | {"data": json.loads(row["data"])}
            for row in self.db.rows(
                "SELECT * FROM audit ORDER BY seq DESC LIMIT ?", (max(1, min(limit, 500)),)
            )
        ]
