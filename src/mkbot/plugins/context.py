"""能力上下文绑定活动、根预算和会话；这不是隔离不可信 Python 代码的沙盒。"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

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

    @property
    def scope(self) -> str:
        return "conversation:" + self.conversation_id if self.conversation_id else "self"

    def read(self, key: str) -> dict[str, Any] | None:
        rows = self._db.rows(
            "SELECT value FROM plugin_values WHERE plugin=? AND scope=? AND key=?",
            (self.plugin_id, self.scope, key),
        )
        return json.loads(rows[0]["value"]) if rows else None

    def write(self, key: str, value: dict[str, Any]) -> None:
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
