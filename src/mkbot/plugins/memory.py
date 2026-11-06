from pydantic import Field

from mkbot.domain.errors import Conflict, NotFound
from mkbot.domain.models import StrictModel
from mkbot.memory.contracts import MemoryProposal
from mkbot.memory.service import MemoryService
from mkbot.plugins.context import PluginContext
from mkbot.plugins.contracts import Manifest, Plugin, Tool, ToolResult


class Recall(StrictModel):
    query: str = Field(default="", max_length=1000)
    subjects: list[str] | None = Field(default=None, max_length=20)


class UseMemory(StrictModel):
    memory_ids: list[str] = Field(min_length=1, max_length=20)
    purpose: str = Field(min_length=1, max_length=500)


async def recall(context: PluginContext, arguments: Recall) -> ToolResult:
    return ToolResult(
        data={
            "items": [
                MemoryService.context_item(row)
                for row in context.recall(arguments.query, arguments.subjects)
            ]
        }
    )


async def remember(context: PluginContext, arguments: MemoryProposal) -> ToolResult:
    try:
        item = context.remember(arguments)
    except (Conflict, NotFound, ValueError) as exc:
        # 来源/范围拒绝发生在事务中，已知没有未确认的外部副作用。
        return ToolResult(ok=False, error=str(exc))
    return ToolResult(
        data={"id": item["id"], "revision": item["revision"], "confidence": item["confidence"]}
    )


async def mark_use(context: PluginContext, arguments: UseMemory) -> ToolResult:
    try:
        context.used_memory(arguments.memory_ids, arguments.purpose)
    except (Conflict, NotFound, ValueError) as exc:
        return ToolResult(ok=False, error=str(exc))
    return ToolResult(data={"usage_claim_saved": True})


def memory_plugin() -> Plugin:
    return Plugin(
        Manifest(
            id="memory",
            name="角色记忆",
            version="0.1.0",
            description="按当前会话范围回想人物偏好、共同经历和自己的长期兴趣；保存有来源的事实或观点。",
            instructions="只使用提供的 source_ref 或 memory:记忆ID 作为依据，不编造来源。"
            "人物 ID 使用来源中的 subject_id，角色自身用 self。"
            "偏好、边界与重要经历可存 long；临时近况用 current_state 并设有效期。"
            "派生摘要不是新证据。"
            "确实用到选入的记忆时标记用途；读取不会增加可信度。无需记住每句闲聊。",
        ),
        (
            Tool("recall", "在当前允许范围内查找相关记忆。", Recall, recall),
            Tool(
                "remember",
                "提议保存带真实来源的记忆；范围自动继承来源。",
                MemoryProposal,
                remember,
                effect="local",
            ),
            Tool(
                "mark_used",
                "标记本轮实际用于结果的记忆，完成后由运行时核对并有限强化。",
                UseMemory,
                mark_use,
                effect="local",
            ),
        ),
    )
