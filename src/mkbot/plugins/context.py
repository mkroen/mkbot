"""能力上下文绑定活动、根预算和会话；这不是隔离不可信 Python 代码的沙盒。"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from mkbot.memory.contracts import MemoryProposal
from mkbot.memory.service import MemoryService
from mkbot.models.contracts import Message, ModelReply
from mkbot.models.gateway import ModelGateway
from mkbot.runtime.clock import Clock, timestamp
from mkbot.storage.database import Database, encode


@dataclass(frozen=True, slots=True)
class PluginContext:
    plugin_id: str
    activity_id: str
    root_id: str
    conversation_id: str | None
    invocation_key: str
    _db: Database
    _clock: Clock
    _models: ModelGateway
    _memory: MemoryService | None = None

    @property
    def scope(self) -> str:
        return "conversation:" + self.conversation_id if self.conversation_id else "self"

    def read(self, key: str) -> dict[str, Any] | None:
        if self._memory:
            refs = self._db.rows(
                "SELECT memory_id AS id,revision FROM plugin_memory_refs WHERE plugin=? AND "
                "scope=? AND key=?",
                (self.plugin_id, self.scope, key),
            )
            self._memory.select_refs(self.activity_id, refs)
            source_refs = self._db.rows(
                "SELECT source_id FROM plugin_source_refs WHERE plugin=? AND scope=? AND key=?",
                (self.plugin_id, self.scope, key),
            )
            self._memory.select_sources(self.activity_id, [row["source_id"] for row in source_refs])
        rows = self._db.rows(
            "SELECT value FROM plugin_values WHERE plugin=? AND scope=? AND key=?",
            (self.plugin_id, self.scope, key),
        )
        return json.loads(rows[0]["value"]) if rows else None

    def write(self, key: str, value: dict[str, Any]) -> None:
        refs = self._memory.references(self.activity_id) if self._memory else []
        sources = self._memory.source_references(self.activity_id) if self._memory else []
        if self._memory:
            self._memory.assert_context(self.activity_id, refs)
        payload = encode(value)
        if not key or len(key) > 200 or len(payload.encode()) > 64_000:
            raise ValueError("插件状态键或内容超过大小限制")
        with self._db.transaction() as tx:
            self._models.lifecycle.check_admission(tx, self.activity_id)
            tx.execute(
                "INSERT INTO plugin_values VALUES(?,?,?,?,?) "
                "ON CONFLICT(plugin,scope,key) DO UPDATE SET "
                "value=excluded.value,updated_at=excluded.updated_at",
                (self.plugin_id, self.scope, key, payload, timestamp(self._clock.now())),
            )
            tx.execute(
                "DELETE FROM plugin_memory_refs WHERE plugin=? AND scope=? AND key=?",
                (self.plugin_id, self.scope, key),
            )
            tx.executemany(
                "INSERT INTO plugin_memory_refs VALUES(?,?,?,?,?)",
                [(self.plugin_id, self.scope, key, ref["id"], ref["revision"]) for ref in refs],
            )
            tx.execute(
                "DELETE FROM plugin_source_refs WHERE plugin=? AND scope=? AND key=?",
                (self.plugin_id, self.scope, key),
            )
            tx.executemany(
                "INSERT INTO plugin_source_refs VALUES(?,?,?,?)",
                [(self.plugin_id, self.scope, key, source) for source in sources],
            )

    def recall(self, query: str, subjects: list[str] | None = None) -> list[dict]:
        if not self._memory:
            raise RuntimeError("记忆服务尚未配置")
        return self._memory.search(
            audience=self.conversation_id or "@self",
            query=query,
            subjects=subjects,
            activity_id=self.activity_id,
            select=True,
        )

    def remember(self, proposal: MemoryProposal) -> dict:
        if not self._memory:
            raise RuntimeError("记忆服务尚未配置")
        item = self._memory.propose(proposal, activity_id=self.activity_id)
        self._memory.select_refs(
            self.activity_id, [{"id": item["id"], "revision": item["revision"]}]
        )
        return item

    def used_memory(self, ids: list[str], purpose: str) -> None:
        if not self._memory:
            raise RuntimeError("记忆服务尚未配置")
        self._memory.claim_use(self.activity_id, ids, purpose)

    async def model(
        self,
        key: str,
        messages: list[Message],
        *,
        role: str = "planner",
        max_output_tokens: int = 1024,
    ) -> ModelReply:
        # 派生键继承活动身份。插件不能换一个根活动获得新的预算。
        stable = hashlib.sha256(
            encode([self.plugin_id, self.invocation_key, key]).encode()
        ).hexdigest()
        return await self._models.call(
            self.activity_id,
            f"plugin:{stable}",
            role,
            messages,
            max_output_tokens=max_output_tokens,
        )
