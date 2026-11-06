"""配置采用显式版本更新；回滚创建新版本，保留真实历史。"""

import json
from typing import Any

from mkbot.domain.errors import Conflict, NotFound
from mkbot.runtime.clock import Clock, timestamp
from mkbot.runtime.lifecycle import audit
from mkbot.storage.database import Database, encode


class ConfigStore:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db, self.clock = db, clock

    def get(self, name: str, revision: int | None = None) -> dict:
        rows = self.db.rows(
            "SELECT * FROM config_revisions WHERE name=? "
            + ("AND revision=? " if revision is not None else "")
            + "ORDER BY revision DESC LIMIT 1",
            (name, revision) if revision is not None else (name,),
        )
        if not rows:
            raise NotFound("配置版本不存在")
        return rows[0] | {"content": json.loads(rows[0]["content"])}

    def put(self, name: str, content: dict[str, Any], expected_revision: int) -> dict:
        if not name or len(name) > 100 or expected_revision < 0:
            raise ValueError("配置名称或期望版本无效")
        payload = encode(content)
        if len(payload.encode()) > 256_000:
            raise ValueError("配置内容过大")
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            latest = tx.execute(
                "SELECT coalesce(max(revision),0) FROM config_revisions WHERE name=?", (name,)
            ).fetchone()[0]
            if latest != expected_revision:
                raise Conflict("配置已经被修改，请重新读取后合并")
            tx.execute(
                "INSERT INTO config_revisions VALUES(?,?,?,?)", (name, latest + 1, payload, now)
            )
            audit(tx, "config.changed", name, {"revision": latest + 1}, now)
        return self.get(name)

    def history(self, name: str) -> list[dict]:
        return self.db.rows(
            "SELECT name,revision,created_at FROM config_revisions WHERE name=? "
            "ORDER BY revision DESC",
            (name,),
        )
