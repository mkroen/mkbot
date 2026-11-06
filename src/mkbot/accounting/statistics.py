"""按请求开始时间统计持久账本；只取全局日账，避免重复累加根活动额度。"""

from datetime import timedelta

from mkbot.runtime.clock import Clock, timestamp
from mkbot.storage.database import Database


def overview(db: Database, clock: Clock, hours: int = 24) -> dict:
    now = clock.now()
    since = now - timedelta(hours=hours)
    args = (timestamp(since), timestamp(now))
    fields = (
        "count(*) AS calls,coalesce(sum(c.status='succeeded'),0) AS succeeded,"
        "coalesce(sum(c.status='failed'),0) AS failed,"
        "coalesce(sum(c.status='running'),0) AS running,"
        "coalesce(sum(c.usage IS NULL AND c.status!='running'),0) AS unknown_usage,"
        "coalesce(sum(json_extract(c.usage,'$.input_tokens')),0) AS input_tokens,"
        "coalesce(sum(json_extract(c.usage,'$.output_tokens')),0) AS output_tokens,"
        "coalesce(sum(json_extract(c.usage,'$.cached_input_tokens')),0) AS "
        "cached_input_tokens,"
        "avg(CASE WHEN c.status='succeeded' THEN c.elapsed_ms END) AS average_ms,"
        "coalesce(sum((SELECT r.actual FROM reservations r WHERE r.id=c.id "
        "AND r.scope LIKE 'day:%' AND r.metric='micro_usd' AND r.state='settled')),0) "
        "AS settled_micro_usd,"
        "coalesce(sum((SELECT r.reserved FROM reservations r WHERE r.id=c.id "
        "AND r.scope LIKE 'day:%' AND r.metric='micro_usd' "
        "AND r.state IN ('reserved','unknown'))),0) AS reserved_micro_usd"
    )
    where = " FROM model_calls c WHERE c.started_at>=? AND c.started_at<=?"
    summary = db.rows("SELECT " + fields + where, args)[0]
    bucket_format = "%Y-%m-%dT%H:00:00+00:00" if hours == 24 else "%Y-%m-%dT00:00:00+00:00"
    series = db.rows(
        "SELECT strftime(?,c.started_at) AS at," + fields + where + " GROUP BY at ORDER BY at",
        (bucket_format, *args),
    )
    buckets = {row["at"]: row for row in series}
    at = since.replace(minute=0, second=0, microsecond=0)
    if hours != 24:
        at = at.replace(hour=0)
    trend = []
    while at <= now:
        key = at.isoformat()
        trend.append(
            buckets.get(
                key,
                {
                    "at": key,
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "succeeded": 0,
                    "failed": 0,
                },
            )
        )
        at += timedelta(hours=1 if hours == 24 else 24)
    return {
        "since": timestamp(since),
        "until": timestamp(now),
        "hours": hours,
        "summary": summary,
        "trend": trend,
    }
