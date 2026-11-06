"""最小示例插件：绑定当前会话的临时笔记，不把笔记冒充长期事实。"""

from pydantic import Field

from mkbot.domain.models import StrictModel
from mkbot.plugins.context import PluginContext
from mkbot.plugins.contracts import Manifest, Plugin, Tool, ToolResult


class ReadNote(StrictModel):
    key: str = Field(min_length=1, max_length=80)


class WriteNote(ReadNote):
    text: str = Field(min_length=1, max_length=4000)


async def read(context: PluginContext, arguments: ReadNote) -> ToolResult:
    return ToolResult(data={"note": context.read(arguments.key)})


async def write(context: PluginContext, arguments: WriteNote) -> ToolResult:
    context.write(arguments.key, {"text": arguments.text})
    return ToolResult(data={"saved": True, "key": arguments.key})


def notebook_plugin() -> Plugin:
    return Plugin(
        Manifest(
            id="notebook",
            name="随手记",
            version="0.1.0",
            description="记录当前活动所在会话的临时想法、创作草稿和待办线索。",
            instructions="笔记用于延续当前话题；不能自动认定为事实或对外发布。",
        ),
        (
            Tool("read", "按键读取当前会话的笔记。", ReadNote, read),
            Tool("write", "保存当前会话的临时笔记。", WriteNote, write, effect="local"),
        ),
    )
