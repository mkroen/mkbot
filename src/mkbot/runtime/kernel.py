"""运行容器统一组装服务，所有入口共享同一个生命周期与数据库。"""

import asyncio
import json
from pathlib import Path

from mkbot.accounting.budgets import BudgetManager
from mkbot.actions.outbox import Outbox
from mkbot.domain.errors import NotFound
from mkbot.memory.contracts import MemorySettings
from mkbot.memory.service import MemoryService
from mkbot.models.contracts import GatewaySettings
from mkbot.models.gateway import ModelGateway
from mkbot.plugins.life import life_plugin
from mkbot.plugins.memory import memory_plugin
from mkbot.plugins.notebook import notebook_plugin
from mkbot.plugins.plans import plans_plugin
from mkbot.plugins.registry import PluginRegistry
from mkbot.runtime.activities import ActivityService
from mkbot.runtime.agent import AgentLoop, ExecutionSettings
from mkbot.runtime.clock import Clock, SystemClock
from mkbot.runtime.coordinator import Coordinator
from mkbot.runtime.inbox import InboxService
from mkbot.runtime.lifecycle import Lifecycle
from mkbot.runtime.plans import PlanService
from mkbot.runtime.scheduling import SchedulerSettings
from mkbot.storage.config import ConfigStore
from mkbot.storage.database import Database
from mkbot.storage.instance_lock import InstanceLock


class Kernel:
    def __init__(self, data_dir: Path, clock: Clock | None = None) -> None:
        self.clock = clock or SystemClock()
        self._changed = asyncio.Event()
        self._stopped = False
        self.instance_lock = InstanceLock(data_dir / "instance.lock")
        self.instance_lock.acquire()
        try:
            self.db = Database(data_dir / "mkbot.sqlite3")
            self.lifecycle = Lifecycle(self.db, self.clock)
            self.activities = ActivityService(self.lifecycle)
            self.inbox = InboxService(self.lifecycle)
            self.inbox.on_change = self.notify
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
            try:
                self.config.get("memory")
            except NotFound:
                self.config.put("memory", MemorySettings().model_dump(mode="json"), 0)
            try:
                self.config.get("scheduler")
            except NotFound:
                self.config.put("scheduler", SchedulerSettings().model_dump(mode="json"), 0)
            self.memory = MemoryService(self.lifecycle, self.config)
            self.plans = PlanService(self)
            self.models = ModelGateway(self.lifecycle, self.config, self.budgets)
            self.models.context_validator = self.assert_context
            self.plugins = PluginRegistry(self.lifecycle, self.models)
            self.plugins.memory = self.memory
            self.plugins.context_validator = self.assert_context
            self.plugins.register(notebook_plugin())
            self.plugins.register(memory_plugin())
            self.outbox.context_validator = self.assert_context
            self.outbox.context_provider = self.memory.references
            self.outbox.on_sent = self.memory.commit_uses
            self.agent = AgentLoop(self)
            self.scheduler = Coordinator(self)
            self.plugins.register(life_plugin(self.scheduler))
            self.plugins.register(plans_plugin(self.plans))
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self.instance_lock.release()
            raise

    def assert_context(self, activity_id: str, refs: list[dict] | None = None) -> None:
        self.memory.assert_context(activity_id, refs)
        self.scheduler.assert_context(activity_id)

    def notify(self) -> None:
        self._changed.set()

    async def run_timers(self) -> None:
        """兼容管理入口；统一由协调器等待来信、唤醒和持久期限。"""
        await self.scheduler.run()

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
