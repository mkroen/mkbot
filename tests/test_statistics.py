from test_model_gateway import MESSAGES, ScriptedProvider, answer, configured

from mkbot.accounting.statistics import overview
from mkbot.models.contracts import ModelReply


async def test_statistics_preserves_unknown_usage_and_does_not_double_count_scopes(kernel, clock):
    provider = ScriptedProvider(answer(), ModelReply(text="未报告用量"))
    activity = configured(kernel, provider)
    await kernel.models.call(activity, "first", "planner", MESSAGES)
    await kernel.models.call(activity, "second", "planner", MESSAGES)
    snapshot = overview(kernel.db, clock)
    summary = snapshot["summary"]
    assert summary["calls"] == summary["succeeded"] == 2
    assert summary["unknown_usage"] == 1
    assert summary["input_tokens"] == 10
    assert summary["output_tokens"] == 5
    assert summary["settled_micro_usd"] == 20
    assert summary["reserved_micro_usd"] > 0
    assert sum(point["calls"] for point in snapshot["trend"]) == 2
    clock.advance(seconds=86401)
    assert overview(kernel.db, clock)["summary"]["calls"] == 0
    assert overview(kernel.db, clock, 168)["summary"]["calls"] == 2
