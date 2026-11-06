"""带持久回执的能力调用；插件不另起后台循环。"""

import asyncio
import hashlib
import re
from uuid import uuid4

from pydantic import ValidationError

from mkbot.domain.errors import Conflict, NotFound
from mkbot.models.contracts import ModelUnavailable, ToolCall, ToolDefinition
from mkbot.models.gateway import ModelGateway
from mkbot.plugins.context import PluginContext
from mkbot.plugins.contracts import Plugin, Tool, ToolOutcomeUnknown, ToolRejected, ToolResult
from mkbot.runtime.clock import timestamp
from mkbot.runtime.lifecycle import Lifecycle, audit
from mkbot.storage.database import encode


class PluginRegistry:
    def __init__(self, lifecycle: Lifecycle, models: ModelGateway) -> None:
        self.lifecycle, self.db, self.clock, self.models = (
            lifecycle,
            lifecycle.db,
            lifecycle.clock,
            models,
        )
        self.plugins: dict[str, Plugin] = {}
        self.tools: dict[str, tuple[Plugin, Tool]] = {}
        with self.db.transaction() as tx:
            tx.execute("UPDATE tool_calls SET status='unknown' WHERE status='running'")

    def register(self, plugin: Plugin) -> None:
        if plugin.manifest.id in self.plugins:
            raise Conflict("插件已经注册")
        names = [tool.name for tool in plugin.tools]
        if len(names) != len(set(names)):
            raise ValueError("插件内存在重复工具名")
        for tool in plugin.tools:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", tool.name):
                raise ValueError("工具名必须使用小写字母、数字或下划线")
            if not 0 < tool.timeout_seconds <= 600:
                raise ValueError("工具超时必须在 0 至 600 秒之间")
        self.plugins[plugin.manifest.id] = plugin
        for tool in plugin.tools:
            self.tools[f"{plugin.manifest.id}__{tool.name}"] = (plugin, tool)

    def catalog(self) -> list[dict]:
        return [
            p.manifest.model_dump() | {"tools": [f"{p.manifest.id}__{t.name}" for t in p.tools]}
            for p in self.plugins.values()
        ]

    def definitions(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name=name,
                description=tool.description,
                parameters=tool.arguments.model_json_schema(),
            )
            for name, (_, tool) in self.tools.items()
        ]

    def instructions(self) -> str:
        return "\n\n".join(
            f"[{p.manifest.name}]\n{p.manifest.description}\n{p.manifest.instructions}"
            for p in self.plugins.values()
        )

    async def execute(self, activity_id: str, key: str, call: ToolCall) -> ToolResult:
        if not key or len(key) > 200:
            raise ValueError("工具调用需要稳定且有限长度的键")
        if call.name not in self.tools:
            raise NotFound("模型请求了不存在的能力")
        plugin, tool = self.tools[call.name]
        try:
            arguments = tool.arguments.model_validate(call.arguments)
        except ValidationError:
            return ToolResult(ok=False, error="工具参数与定义不符，请修正参数")
        fingerprint = hashlib.sha256(
            encode({"name": call.name, "args": call.arguments}).encode()
        ).hexdigest()
        with self.db.transaction() as tx:
            self.lifecycle.check_admission(tx, activity_id)
            rows = tx.execute(
                "SELECT * FROM tool_calls WHERE activity_id=? AND invocation_key=?",
                (activity_id, key),
            ).fetchall()
            if rows:
                row = rows[0]
                if row["fingerprint"] != fingerprint:
                    raise Conflict("同一工具调用键不能变更操作或参数")
                if row["status"] in ("running", "unknown"):
                    raise ToolOutcomeUnknown("工具尚在执行或结果未知，不能重复执行")
                return ToolResult.model_validate_json(row["result"])
        item = self.db.rows("SELECT * FROM activities WHERE id=?", (activity_id,))[0]
        context = PluginContext(
            plugin.manifest.id,
            activity_id,
            item["root_id"],
            item["conversation_id"],
            key,
            self.db,
            self.clock,
            self.models,
        )
        call_id = str(uuid4())
        work_id = self.lifecycle.start_work("tool", activity_id)
        with self.db.transaction() as tx:
            tx.execute(
                "INSERT INTO tool_calls(id,activity_id,invocation_key,fingerprint,tool,work_id,"
                "status,started_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    call_id,
                    activity_id,
                    key,
                    fingerprint,
                    call.name,
                    work_id,
                    "running",
                    timestamp(self.clock.now()),
                ),
            )
            audit(
                tx,
                "tool.started",
                call_id,
                {"tool": call.name, "activity_id": activity_id},
                timestamp(self.clock.now()),
            )
        try:
            async with asyncio.timeout(tool.timeout_seconds):
                result = ToolResult.model_validate(await tool.handler(context, arguments))
                if len(result.model_dump_json().encode()) > 64_000:
                    raise ValueError("工具结果超过大小限制")
        except ToolRejected:
            result = ToolResult(ok=False, error="工具已确认此次操作未执行")
            self._finish(call_id, work_id, "failed", result)
        except ModelUnavailable:
            self._finish(call_id, work_id, "unknown", None)
            raise ToolOutcomeUnknown("插件中的模型调用未完成，需要核对后继续") from None
        except BaseException as exc:
            if tool.effect != "read_only":
                self._finish(call_id, work_id, "unknown", None)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise ToolOutcomeUnknown("工具中断，外部结果待核对") from None
            result = ToolResult(ok=False, error="工具读取失败")
            self._finish(call_id, work_id, "failed", result)
            if isinstance(exc, asyncio.CancelledError):
                raise
        else:
            self._finish(call_id, work_id, "succeeded" if result.ok else "failed", result)
        return result

    def _finish(self, call_id: str, work_id: str, status: str, result: ToolResult | None) -> None:
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            tx.execute(
                "UPDATE tool_calls SET status=?,result=?,finished_at=? WHERE id=?",
                (status, result.model_dump_json() if result else None, now, call_id),
            )
            audit(tx, "tool.finished", call_id, {"status": status}, now)
        self.lifecycle.finish_work(work_id, status)
