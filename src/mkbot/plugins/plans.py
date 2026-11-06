"""目标是角色的一种内置能力，所有轮次仍由主会话调度。"""

from mkbot.domain.errors import AdmissionDenied, Conflict, NotFound
from mkbot.plugins.contracts import Manifest, Plugin, Tool, ToolResult
from mkbot.runtime.planning import PlanOutcome, PlanProposal


def plans_plugin(plans) -> Plugin:
    async def create(context, arguments):
        try:
            item = plans.create(
                arguments, activity_id=context.activity_id, key=context.invocation_key
            )
            return ToolResult(
                data={"id": item["id"], "goal": item["goal"], "due_at": item["due_at"]}
            )
        except (AdmissionDenied, Conflict, NotFound, ValueError) as exc:
            return ToolResult(ok=False, error=str(exc))

    async def progress(context, arguments):
        try:
            return ToolResult(data=plans.choose(context.activity_id, arguments))
        except (AdmissionDenied, Conflict, NotFound, ValueError) as exc:
            return ToolResult(ok=False, error=str(exc))

    return Plugin(
        Manifest(
            id="plans",
            name="目标与计划",
            version="0.1.0",
            description="保存长期目标、明确下一步，在未来的活动中继续推进。",
            instructions="create 仅在确有必要时建立目标，已有目标不要反复重建。"
            "目标保留原会话的隐私范围。自己的内部目标只用于自己的活动；"
            "私聊约定只回到对应私聊。未给 due_at 仅记录，不自动运行。"
            "plan_step 活动结束时必须使用 progress 报告实际进展，"
            "并选择 continue/pause/completed/cancelled。"
            "continue 需给未来时间和具体下一步。没有实际进展时选择 pause，"
            "不能换名或创建子目标逃避限制。"
            "progress 会结束本轮且不发送消息；模型陈述的进度不是工具已成功的证明。",
        ),
        (
            Tool(
                "create",
                "保存一个目标；继承当前受众、资料来源与根预算。",
                PlanProposal,
                create,
                effect="local",
            ),
            Tool(
                "progress",
                "结束当前计划执行并保存进度及下一步。",
                PlanOutcome,
                progress,
                effect="local",
            ),
        ),
    )
