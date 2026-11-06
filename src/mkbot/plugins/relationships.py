"""关系判断进入同一个主会话，内核验证互动和提交条件。"""

from mkbot.character.relationships import RelationshipChange
from mkbot.domain.errors import AdmissionDenied, Conflict, NotFound
from mkbot.plugins.contracts import Manifest, Plugin, Tool, ToolResult


def relationships_plugin(relationships) -> Plugin:
    async def reflect(context, arguments):
        try:
            return ToolResult(data=relationships.propose(context.activity_id, arguments))
        except (AdmissionDenied, Conflict, NotFound, ValueError) as exc:
            return ToolResult(ok=False, error=str(exc))

    return Plugin(
        Manifest(
            id="relationships",
            name="人物关系",
            version="0.1.0",
            description="根据本轮真实相处，审慎更新熟悉度、亲近倾向和信任。",
            instructions="无需每轮改变关系。只有实际互动带来变化时才用 reflect，说明具体依据。"
            "subject_id 使用本轮资料提供的平台/用户 ID，不要因昵称相同而合并人物。"
            "熟悉度 0～100，亲近倾向与信任 -100～100，0 表示尚无明显倾向。"
            "默认单次熟悉度至多增加 2，亲近与信任各变化不超过 5；不能靠反复读取加分。"
            "提出的变化先等待本轮处理完成，回复需要确认投递后生效。"
            "关系理由和人物边界只适用于给定受众；关系分数不是事实证明或管理权限。"
            "熟人表情可以不回，陌生人的明确请求仍应认真考虑。",
        ),
        (
            Tool(
                "reflect",
                "根据本轮分配的真实留言，提出有限关系变化。",
                RelationshipChange,
                reflect,
                effect="local",
            ),
        ),
    )
