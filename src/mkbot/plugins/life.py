"""角色通过普通能力入口提交决定，生命周期和收件状态仍由内核落实。"""

from typing import TYPE_CHECKING

from mkbot.domain.errors import Conflict, NotFound
from mkbot.domain.models import SleepRequest
from mkbot.plugins.contracts import Manifest, Plugin, Tool, ToolResult
from mkbot.runtime.scheduling import ChatChoice, IdleWait

if TYPE_CHECKING:
    from mkbot.runtime.coordinator import Coordinator


def life_plugin(coordinator: "Coordinator") -> Plugin:
    def handler(method):
        async def invoke(context, arguments):
            try:
                return ToolResult(data=method(context.activity_id, arguments))
            except (Conflict, NotFound, ValueError) as exc:
                return ToolResult(ok=False, error=str(exc))

        return invoke

    return Plugin(
        Manifest(
            id="life",
            name="日常安排",
            version="0.1.0",
            description="对分配的留言选择是否回应；在自己的活动结束时决定等待或睡觉。",
            instructions="对当前分配的一批留言可用 review 选择 reply/no_reply/defer/expired。"
            "连续消息合在一起考虑，不需要每条都回应；留意后来的取消、解决和时效。"
            "reply 的 text 是唯一的最终回复，其他选择不会发消息。"
            "自己的活动可以用 wait_until 等待，或 sleep 保存明确唤醒窗口。"
            "这些决定会结束本轮，不能在同一轮后面追加其他工具。",
        ),
        (
            Tool(
                "review",
                "处置当前活动绑定的整批留言，保存理由和可选的回复。",
                ChatChoice,
                handler(coordinator.choose_chat),
                effect="local",
            ),
            Tool(
                "wait_until",
                "结束自己的本轮活动，等待到指定时间再考虑空闲行动。",
                IdleWait,
                handler(coordinator.choose_wait),
                effect="local",
            ),
            Tool(
                "sleep",
                "结束自己的本轮活动并提出睡眠计划，内核会等待全部在途工作收尾。",
                SleepRequest,
                handler(coordinator.choose_sleep),
                effect="local",
            ),
        ),
    )
