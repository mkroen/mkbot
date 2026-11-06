"""来源是事实依据，活跃度是召回信号；二者不会互相替代。"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from uuid import uuid4

from mkbot.domain.errors import Conflict, ContextInvalidated, NotFound
from mkbot.memory.contracts import (
    ManualMemory,
    MemoryEdit,
    MemoryProposal,
    MemoryRef,
    MemorySettings,
)
from mkbot.runtime.clock import timestamp
from mkbot.runtime.lifecycle import Lifecycle, audit
from mkbot.storage.config import ConfigStore
from mkbot.storage.database import encode


def allows(scope: list[str], audience: str) -> bool:
    return "*" in scope or audience in scope


def subset(proposed: list[str], original: list[str]) -> bool:
    return "*" in original or ("*" not in proposed and set(proposed) <= set(original))


def intersection(scopes: list[list[str]]) -> list[str]:
    current = {"*"}
    for scope in scopes:
        if "*" in current:
            current = set(scope)
        elif "*" not in scope:
            current &= set(scope)
    if not current:
        raise Conflict("这些来源没有共同可用范围，请分别保存")
    return sorted(current)


class MemoryService:
    def __init__(self, lifecycle: Lifecycle, config: ConfigStore) -> None:
        self.lifecycle, self.db, self.clock, self.config = (
            lifecycle,
            lifecycle.db,
            lifecycle.clock,
            config,
        )

    def settings(self) -> MemorySettings:
        return MemorySettings.model_validate(self.config.get("memory")["content"])

    def _row(self, tx, memory_id: str) -> dict:
        row = tx.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        if not row:
            raise NotFound("记忆不存在")
        result = dict(row)
        for key in ("subjects", "use_scope", "disclosure_scope"):
            result[key] = json.loads(result[key])
        result["source_refs"] = [
            r[0]
            for r in tx.execute(
                "SELECT source_id FROM memory_evidence WHERE memory_id=? ORDER BY source_id",
                (memory_id,),
            )
        ]
        return result

    def get(self, memory_id: str) -> dict:
        with self.db.transaction() as tx:
            row = self._row(tx, memory_id)
            return row | {"current_activation": self._activation(row, self.settings())}

    def list(
        self,
        limit: int = 200,
        *,
        offset: int = 0,
        subject: str | None = None,
        tier: str | None = None,
        status: str | None = None,
    ) -> list[dict]:
        conditions, parameters = [], []
        if subject:
            conditions.append("EXISTS (SELECT 1 FROM json_each(subjects) WHERE value=?)")
            parameters.append(subject)
        for field, value in (("tier", tier), ("status", status)):
            if value:
                conditions.append(f"{field}=?")
                parameters.append(value)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.db.transaction() as tx:
            ids = [
                r[0]
                for r in tx.execute(
                    "SELECT id FROM memories"
                    + where
                    + " ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?",
                    (*parameters, max(1, min(limit, 1000)), max(0, offset)),
                )
            ]
            settings = self.settings()
            rows = [self._row(tx, key) for key in ids]
            return [row | {"current_activation": self._activation(row, settings)} for row in rows]

    def history(self, memory_id: str) -> list[dict]:
        self.get(memory_id)
        return [
            json.loads(r["snapshot"])
            for r in self.db.rows(
                "SELECT snapshot FROM memory_versions WHERE memory_id=? ORDER BY revision",
                (memory_id,),
            )
        ]

    def _version(self, tx, memory_id: str) -> None:
        row = self._row(tx, memory_id)
        tx.execute(
            "INSERT INTO memory_versions VALUES(?,?,?,?)",
            (memory_id, row["revision"], encode(row), timestamp(self.clock.now())),
        )

    def operator_source(self, content: str, *, scope: list[str], actor_id: str = "operator") -> str:
        with self.db.transaction() as tx:
            return self._operator_source(tx, content, scope=scope, actor_id=actor_id)

    def _operator_source(
        self, tx, content: str, *, scope: list[str], actor_id: str = "operator"
    ) -> str:
        from mkbot.memory.contracts import valid_scope

        scope = valid_scope(scope)
        if not content or len(content) > 8000:
            raise ValueError("手动来源内容为空或过长")
        source_id = "operator:" + str(uuid4())
        now = timestamp(self.clock.now())
        tx.execute(
            "INSERT INTO memory_sources VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                source_id,
                "operator",
                actor_id,
                None,
                encode(scope),
                encode(scope),
                now,
                1,
                content,
                now,
            ),
        )
        return source_id

    def _source(self, tx, source_id: str) -> dict:
        if tx.execute(
            "SELECT 1 FROM memory_suppression WHERE source_id=?", (source_id,)
        ).fetchone():
            raise Conflict("该来源已被遗忘，禁止从旧来源重新提炼")
        row = tx.execute("SELECT * FROM memory_sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            now = timestamp(self.clock.now())
            if source_id.startswith("event:"):
                event = tx.execute(
                    "SELECT e.*,i.status FROM events e JOIN inbox i ON e.id=i.event_id "
                    "WHERE "
                    "e.id=?",
                    (source_id[6:],),
                ).fetchone()
                if not event or event["status"] == "ignored":
                    raise NotFound("事件来源不存在或属于自身回声")
                scope = (
                    ["*"]
                    if event["kind"] == "group" and self.settings().share_group_memories
                    else [event["conversation_id"]]
                )
                row = (
                    source_id,
                    "event",
                    f"{event['source']}/{event['sender_id']}",
                    event["conversation_id"],
                    encode(scope),
                    encode(scope),
                    event["occurred_at"],
                    1,
                    None,
                    now,
                )
            elif source_id.startswith("tool:"):
                tool = tx.execute(
                    "SELECT t.*,a.conversation_id FROM tool_calls t JOIN activities a ON "
                    "a.id=t.activity_id WHERE t.id=?",
                    (source_id[5:],),
                ).fetchone()
                if not tool or tool["status"] != "succeeded" or tool["evidence_kind"] == "none":
                    raise NotFound("没有成功的工具回执作为来源")
                scope = [tool["conversation_id"] or "@self"]
                # 工具回执证明工具报告了结果，不自动证明报告内容是真实世界事实。
                row = (
                    source_id,
                    "tool_report",
                    "self",
                    tool["conversation_id"],
                    encode(scope),
                    encode(scope),
                    tool["finished_at"],
                    1,
                    None,
                    now,
                )
            else:
                raise NotFound("来源不存在")
            tx.execute("INSERT INTO memory_sources VALUES(?,?,?,?,?,?,?,?,?,?)", row)
            row = tx.execute("SELECT * FROM memory_sources WHERE id=?", (source_id,)).fetchone()
        return dict(row) | {
            "use_scope": json.loads(row["use_scope"]),
            "disclosure_scope": json.loads(row["disclosure_scope"]),
        }

    def _audience(self, tx, activity_id: str) -> str:
        row = tx.execute(
            "SELECT conversation_id FROM activities WHERE id=?", (activity_id,)
        ).fetchone()
        if not row:
            raise NotFound("活动不存在")
        return row[0] or "@self"

    def _usable(self, row: dict, audience: str) -> bool:
        now = timestamp(self.clock.now())
        return (
            row["status"] == "active"
            and row["valid_from"] <= now
            and (row["valid_until"] is None or row["valid_until"] > now)
            and allows(row["use_scope"], audience)
            and (audience == "@self" or allows(row["disclosure_scope"], audience))
        )

    def propose(
        self, proposal: MemoryProposal, *, activity_id: str | None = None, operator: bool = False
    ) -> dict:
        with self.db.transaction() as tx:
            memory_id = self._propose(tx, proposal, activity_id=activity_id, operator=operator)
        return self.get(memory_id)

    def create_manual(self, request: ManualMemory) -> dict:
        with self.db.transaction() as tx:
            source_id = self._operator_source(tx, request.content, scope=request.scope)
            proposal = MemoryProposal(
                **request.model_dump(exclude={"scope"}), source_refs=[source_id]
            )
            memory_id = self._propose(tx, proposal, operator=True)
        return self.get(memory_id)

    def _propose(
        self,
        tx,
        proposal: MemoryProposal,
        *,
        activity_id: str | None = None,
        operator: bool = False,
    ) -> str:
        if not operator and not activity_id:
            raise ValueError("记忆提议需要真实活动或操作者身份")
        now = timestamp(self.clock.now())
        if proposal.valid_until and timestamp(proposal.valid_until) <= now:
            raise ValueError("新记忆的有效期已经结束")
        memory_id = str(uuid4())
        audience = self._audience(tx, activity_id) if activity_id else "@self"
        if not operator:
            self.lifecycle.check_admission(tx, activity_id)
        roots, parents, use_scopes, disclosure_scopes = {}, [], [], []
        for ref in proposal.source_refs:
            if ref.startswith("memory:"):
                parent = self._row(tx, ref[7:])
                if not self._usable(parent, audience) and not operator:
                    raise Conflict("派生记忆来源不在当前可用范围")
                if parent["status"] != "active":
                    raise Conflict("不能从失效记忆生成新的记录")
                if parent["valid_until"] and parent["valid_until"] <= now:
                    raise Conflict("不能从已过期记忆生成新的记录")
                parents.append(parent)
                use_scopes.append(parent["use_scope"])
                disclosure_scopes.append(parent["disclosure_scope"])
                for root in parent["source_refs"]:
                    roots[root] = self._source(tx, root)
            else:
                source = self._source(tx, ref)
                roots[ref] = source
                use_scopes.append(source["use_scope"])
                disclosure_scopes.append(source["disclosure_scope"])
        inherited_use, inherited_disclosure = (
            intersection(use_scopes),
            intersection(disclosure_scopes),
        )
        use_scope = proposal.use_scope or inherited_use
        disclosure_scope = proposal.disclosure_scope or inherited_disclosure
        if not subset(use_scope, inherited_use) or not subset(
            disclosure_scope, inherited_disclosure
        ):
            raise Conflict("新提议不能扩大来源范围；显式共享请通过管理编辑")
        if not operator and (
            not allows(use_scope, audience)
            or (audience != "@self" and not allows(disclosure_scope, audience))
        ):
            raise Conflict("来源不允许用于当前会话")
        if not roots or not any(source["independent"] for source in roots.values()):
            raise Conflict("记忆没有独立来源")
        kinds = {source["kind"] for source in roots.values()}
        actors = {source["actor_id"] for source in roots.values()}
        confidence = (
            "inferred"
            if proposal.kind in ("opinion", "inference")
            else (
                "operator"
                if kinds == {"operator"}
                else "tool_report"
                if kinds == {"tool_report"}
                else "self_report"
                if len(proposal.subjects) == 1 and actors == set(proposal.subjects)
                else "third_party"
            )
        )
        # 摘要只继承证据状态，不能把模型推断洗成本人确认。
        if any(parent["confidence"] == "inferred" for parent in parents):
            confidence = "inferred"
        elif any(parent["confidence"] == "operator" for parent in parents):
            confidence = "operator"
        occurred = max(source["occurred_at"] for source in roots.values())
        origin = next(iter(roots.values()))["conversation_id"] or "@self"
        expiries = [parent["valid_until"] for parent in parents if parent["valid_until"]]
        if proposal.valid_until:
            expiries.append(timestamp(proposal.valid_until))
        valid_until = min(expiries) if expiries else None
        tx.execute(
            "INSERT INTO "
            "memories(id,kind,subjects,content,tier,origin_scope,use_scope,disclosure_scope,"
            "confidence,importance,occurred_at,valid_from,valid_until,activation_at,created_at,upd"
            "ated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                memory_id,
                proposal.kind,
                encode(proposal.subjects),
                proposal.content,
                proposal.tier,
                origin,
                encode(use_scope),
                encode(disclosure_scope),
                confidence,
                proposal.importance,
                occurred,
                occurred,
                valid_until,
                now,
                now,
                now,
            ),
        )
        tx.executemany(
            "INSERT INTO memory_evidence VALUES(?,?)", [(memory_id, source) for source in roots]
        )
        tx.executemany(
            "INSERT INTO memory_derivations VALUES(?,?)",
            [(parent["id"], memory_id) for parent in parents],
        )
        if proposal.supersedes:
            old = self._row(tx, proposal.supersedes)
            if old["revision"] != proposal.expected_revision or old["status"] != "active":
                raise Conflict("待替代记忆的版本或状态已变化")
            if (
                old["subjects"] != proposal.subjects
                or confidence not in ("self_report", "operator")
                or occurred < old["occurred_at"]
            ):
                raise Conflict("替代需要同一人物的较新本人陈述或人工核实")
            if not operator and not self._usable(old, audience):
                raise Conflict("不能替代当前范围外的记忆")
            tx.execute(
                "UPDATE memories SET "
                "status='superseded',superseded_by=?,revision=revision+1,updated_at=? "
                "WHERE id=?",
                (memory_id, now, old["id"]),
            )
            descendants = self._descendants(tx, [old["id"]])
            for child in descendants - {old["id"]}:
                tx.execute(
                    "UPDATE memories SET "
                    "status='superseded',revision=revision+1,updated_at=? "
                    "WHERE id=?",
                    (now, child),
                )
                self._version(tx, child)
            self._version(tx, old["id"])
            self._invalidate(tx, descendants, "记忆已被更正或替代")
        self._version(tx, memory_id)
        audit(
            tx,
            "memory.created",
            memory_id,
            {"kind": proposal.kind, "root_sources": len(roots)},
            now,
        )
        self._maintain(tx)
        return memory_id

    def _descendants(self, tx, ids: list[str] | set[str]) -> set[str]:
        found, pending = set(ids), list(ids)
        while pending:
            parent = pending.pop()
            for row in tx.execute(
                "SELECT child_id FROM memory_derivations WHERE parent_id=?", (parent,)
            ):
                if row[0] not in found:
                    found.add(row[0])
                    pending.append(row[0])
        return found

    def _invalidate(self, tx, ids: set[str], reason: str) -> None:
        affected = set()
        for memory_id in ids:
            affected.update(
                r[0]
                for r in tx.execute(
                    "SELECT DISTINCT activity_id FROM memory_selections WHERE memory_id=?",
                    (memory_id,),
                )
            )
            for row in tx.execute(
                "SELECT plugin,scope,key FROM plugin_memory_refs WHERE memory_id=?", (memory_id,)
            ).fetchall():
                tx.execute(
                    "DELETE FROM plugin_values WHERE plugin=? AND scope=? AND key=?", tuple(row)
                )
                tx.execute(
                    "DELETE FROM plugin_memory_refs WHERE plugin=? AND scope=? AND key=?",
                    tuple(row),
                )
                tx.execute(
                    "DELETE FROM plugin_source_refs WHERE plugin=? AND scope=? AND key=?",
                    tuple(row),
                )
        self._invalidate_activities(tx, affected, reason)

    def _invalidate_activities(self, tx, affected: set[str], reason: str) -> None:
        now = timestamp(self.clock.now())
        for activity_id in affected:
            tx.execute(
                "INSERT OR REPLACE INTO context_invalidations VALUES(?,?,?)",
                (activity_id, reason, now),
            )
            tx.execute(
                "UPDATE activities SET checkpoint='{}',version=version+1,updated_at=? WHERE id=?",
                (now, activity_id),
            )
            tx.execute(
                "UPDATE activity_decisions SET payload='{}',status='invalidated' WHERE "
                "activity_id=?",
                (activity_id,),
            )
            tx.execute(
                "UPDATE model_calls SET response=NULL,status=CASE WHEN "
                "status='succeeded' THEN "
                "'invalidated' ELSE status END WHERE activity_id=?",
                (activity_id,),
            )
            tx.execute(
                "UPDATE tool_calls SET result=NULL,status=CASE WHEN status IN "
                "('succeeded','failed') THEN 'invalidated' ELSE status END WHERE "
                "activity_id=?",
                (activity_id,),
            )
            tx.execute(
                "UPDATE deliveries SET payload='{}',status=CASE WHEN status='pending' THEN "
                "'cancelled' ELSE status END,error=?,updated_at=? WHERE activity_id=?",
                (reason, now, activity_id),
            )
            audit(tx, "context.invalidated", activity_id, {"reason": reason}, now)

    def revise(self, memory_id: str, edit: MemoryEdit) -> dict:
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            old = self._row(tx, memory_id)
            if old["revision"] != edit.expected_revision or old["status"] != "active":
                raise Conflict("记忆版本已变化或不再有效")
            for field in ("use_scope", "disclosure_scope"):
                value = getattr(edit, field)
                if value and not subset(value, old[field]) and not edit.allow_wider_sharing:
                    raise Conflict("扩大共享范围需要明确启用 allow_wider_sharing")
            values = {
                key: value
                for key, value in edit.model_dump(exclude_unset=True).items()
                if key not in ("expected_revision", "allow_wider_sharing")
            }
            if old["kind"] == "current_state" and "valid_until" in values:
                if values["valid_until"] is None:
                    raise ValueError("临时近况不能清空有效期")
            if not values:
                return old
            for key, value in values.items():
                if key in ("use_scope", "disclosure_scope"):
                    value = encode(value)
                if key == "valid_until":
                    value = timestamp(value) if value else None
                tx.execute(f"UPDATE memories SET {key}=? WHERE id=?", (value, memory_id))
            if "content" in values and values["content"] != old["content"]:
                # 人工改写仍保留原根来源与修订链，但不再标成用户本人原始陈述。
                tx.execute("UPDATE memories SET confidence='operator' WHERE id=?", (memory_id,))
            tx.execute(
                "UPDATE memories SET revision=revision+1,updated_at=? WHERE id=?", (now, memory_id)
            )
            affected = self._descendants(tx, [memory_id])
            # 旧派生摘要不能在父事实改变后继续冒充当前事实。
            for child in affected - {memory_id}:
                tx.execute(
                    "UPDATE memories SET "
                    "status='superseded',revision=revision+1,updated_at=? "
                    "WHERE id=?",
                    (now, child),
                )
                self._version(tx, child)
            self._version(tx, memory_id)
            self._invalidate(tx, affected, "记忆内容或共享规则已修改")
            audit(
                tx,
                "memory.revised",
                memory_id,
                {"revision": old["revision"] + 1, "sharing_override": edit.allow_wider_sharing},
                now,
            )
            self._maintain(tx)
        return self.get(memory_id)

    def forget(self, memory_id: str, expected_revision: int) -> dict:
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            item = self._row(tx, memory_id)
            if item["revision"] != expected_revision:
                raise Conflict("记忆版本已变化")
            affected = {memory_id}
            raw_activities = set()
            for source in item["source_refs"]:
                tx.execute("INSERT OR IGNORE INTO memory_suppression VALUES(?,?)", (source, now))
                tx.execute("UPDATE memory_sources SET content=NULL WHERE id=?", (source,))
                affected.update(
                    r[0]
                    for r in tx.execute(
                        "SELECT memory_id FROM memory_evidence WHERE source_id=?", (source,)
                    )
                )
                raw_activities.update(
                    row[0]
                    for row in tx.execute(
                        "SELECT activity_id FROM activity_sources WHERE source_id=?", (source,)
                    )
                )
                for row in tx.execute(
                    "SELECT plugin,scope,key FROM plugin_source_refs WHERE source_id=?", (source,)
                ).fetchall():
                    for table in ("plugin_values", "plugin_source_refs", "plugin_memory_refs"):
                        tx.execute(
                            f"DELETE FROM {table} WHERE plugin=? AND scope=? AND key=?", tuple(row)
                        )
            affected = self._descendants(tx, affected)
            self._invalidate(tx, affected, "引用的记忆已被遗忘")
            self._invalidate_activities(tx, raw_activities, "引用的原始来源已被遗忘")
            for key in affected:
                tx.execute(
                    "UPDATE memories SET "
                    "content='',subjects='[]',status='forgotten',revision=revision+1,activation=0,"
                    "last_used_at=NULL,protected_until=NULL,updated_at=? WHERE id=?",
                    (now, key),
                )
                tx.execute("DELETE FROM memory_versions WHERE memory_id=?", (key,))
                tx.execute("DELETE FROM memory_uses WHERE memory_id=?", (key,))
                tx.execute("DELETE FROM memory_use_triggers WHERE memory_id=?", (key,))
                tx.execute(
                    "UPDATE memory_selections SET claimed_purpose=NULL WHERE memory_id=?", (key,)
                )
                self._version(tx, key)
            audit(
                tx,
                "memory.forgotten",
                memory_id,
                {"affected_count": len(affected), "suppressed_sources": len(item["source_refs"])},
                now,
            )
        return {
            "affected_ids": sorted(affected),
            "source_reextraction_blocked": True,
            "original_chat_retained": True,
        }

    def forget_preview(self, memory_id: str) -> dict:
        with self.db.transaction() as tx:
            item = self._row(tx, memory_id)
            affected = {memory_id}
            for source in item["source_refs"]:
                affected.update(
                    row[0]
                    for row in tx.execute(
                        "SELECT memory_id FROM memory_evidence WHERE source_id=?", (source,)
                    )
                )
            affected = self._descendants(tx, affected)
            return {
                "id": memory_id,
                "expected_revision": item["revision"],
                "affected_ids": sorted(affected),
                "source_count": len(item["source_refs"]),
                "original_chat_retained": True,
                "source_reextraction_blocked": True,
            }

    def _activation(self, row: dict, settings: MemorySettings) -> float:
        elapsed = max(
            0, (self.clock.now() - datetime.fromisoformat(row["activation_at"])).total_seconds()
        )
        return row["activation"] * math.pow(2, -elapsed / (settings.half_life_hours * 3600))

    def _maintain(self, tx) -> None:
        settings, now = self.settings(), timestamp(self.clock.now())
        tx.execute(
            "UPDATE memories SET bucket='fifo',protected_until=NULL WHERE tier='recent' "
            "AND "
            "bucket='protected' AND protected_until<=?",
            (now,),
        )
        tx.execute(
            "UPDATE memories SET tier='archived',bucket='fifo',protected_until=NULL WHERE "
            "tier='recent' AND valid_until<=?",
            (now,),
        )
        for bucket, total, per_scope in (
            ("protected", settings.protected_capacity, settings.protected_per_scope),
            ("fifo", settings.recent_capacity, settings.recent_per_scope),
        ):
            while True:
                rows = tx.execute(
                    "SELECT id,origin_scope FROM memories WHERE status='active' AND "
                    "tier='recent' "
                    "AND bucket=? ORDER BY created_at,rowid",
                    (bucket,),
                ).fetchall()
                counts = {}
                for row in rows:
                    counts[row["origin_scope"]] = counts.get(row["origin_scope"], 0) + 1
                crowded = {scope for scope, count in counts.items() if count > per_scope}
                if not crowded and len(rows) <= total:
                    break
                if not crowded:
                    largest = max(counts.values())
                    crowded = {scope for scope, count in counts.items() if count == largest}
                victim = next(row for row in rows if row["origin_scope"] in crowded)
                tx.execute(
                    "UPDATE memories SET tier=?,bucket='fifo',protected_until=NULL WHERE id=?",
                    ("recent" if bucket == "protected" else "archived", victim["id"]),
                )

    def maintain(self, activity_id: str | None = None) -> None:
        with self.db.transaction() as tx:
            self.lifecycle.check_admission(tx, activity_id)
            self._maintain(tx)

    def search(
        self,
        *,
        audience: str,
        query: str = "",
        subjects: list[str] | None = None,
        activity_id: str | None = None,
        select: bool = False,
        limit: int | None = None,
    ) -> list[dict]:
        settings = self.settings()
        limit = min(limit or settings.context_max_items, settings.context_max_items)
        terms = set(re.findall(r"[a-zA-Z0-9_]+", query.casefold()))
        chinese = "".join(re.findall(r"[\u4e00-\u9fff]", query[:300]))
        terms.update(chinese[i : i + 2] for i in range(len(chinese) - 1))
        with self.db.transaction() as tx:
            if select:
                if not activity_id or audience != self._audience(tx, activity_id):
                    raise Conflict("选入上下文必须绑定实际活动与受众")
                self.lifecycle.check_admission(tx, activity_id)
                self._assert_context(tx, activity_id)
            candidates = []
            for source in tx.execute(
                "SELECT id FROM memories WHERE status='active' AND tier IN ('recent','long')"
            ).fetchall():
                row = self._row(tx, source[0])
                if not self._usable(row, audience) or (
                    subjects and not set(subjects) & set(row["subjects"])
                ):
                    continue
                matches = sum(term in row["content"].casefold() for term in terms)
                activation = self._activation(row, settings)
                row["recall_score"] = (
                    matches * 2
                    + row["importance"]
                    + min(activation, 2) * 0.2
                    + (row["tier"] == "long") * 0.1
                )
                row["current_activation"] = activation
                candidates.append(row)
            candidates.sort(
                key=lambda r: (r["recall_score"], r["occurred_at"], r["id"]), reverse=True
            )
            selected, roots, size = [], set(), 0
            for row in candidates:
                sources = set(row["source_refs"])
                if sources <= roots:
                    continue
                payload_size = len(encode(self.context_item(row)))
                if size + payload_size > settings.context_max_chars:
                    continue
                selected.append(row)
                roots |= sources
                size += payload_size
                if select:
                    tx.execute(
                        "INSERT OR IGNORE INTO "
                        "memory_selections(activity_id,memory_id,revision) "
                        "VALUES(?,?,?)",
                        (activity_id, row["id"], row["revision"]),
                    )
                if len(selected) >= limit:
                    break
            return selected

    @staticmethod
    def context_item(row: dict) -> dict:
        return {
            key: row[key]
            for key in (
                "id",
                "revision",
                "kind",
                "subjects",
                "content",
                "confidence",
                "occurred_at",
                "valid_until",
                "source_refs",
            )
        }

    def profile(self, subject: str, audience: str) -> dict:
        with self.db.transaction() as tx:
            rows = tx.execute(
                "SELECT id FROM memories WHERE status='active' AND tier='long' "
                "AND EXISTS (SELECT 1 FROM json_each(subjects) WHERE value=?) "
                "ORDER BY occurred_at DESC,rowid DESC",
                (subject,),
            ).fetchall()
            facts = [self._row(tx, row["id"]) for row in rows]
            facts = [row for row in facts if self._usable(row, audience)]
        return {
            "subject": subject,
            "audience": audience,
            "items": [self.context_item(row) for row in facts],
            "independent_sources": len({source for row in facts for source in row["source_refs"]}),
        }

    def references(self, activity_id: str) -> list[dict]:
        return [
            {"id": row["memory_id"], "revision": row["revision"]}
            for row in self.db.rows(
                "SELECT memory_id,revision FROM memory_selections WHERE activity_id=? "
                "ORDER BY "
                "memory_id,revision",
                (activity_id,),
            )
        ]

    def source_references(self, activity_id: str) -> list[str]:
        return [
            row["source_id"]
            for row in self.db.rows(
                "SELECT source_id FROM activity_sources WHERE activity_id=? ORDER BY source_id",
                (activity_id,),
            )
        ]

    def select_sources(self, activity_id: str, source_refs: list[str]) -> None:
        """原始上下文也有来源依赖，尚未提炼为记忆时同样响应遗忘。"""
        with self.db.transaction() as tx:
            self.lifecycle.check_admission(tx, activity_id)
            self._assert_context(tx, activity_id)
            audience = self._audience(tx, activity_id)
            for source_id in set(source_refs):
                source = self._source(tx, source_id)
                if not allows(source["use_scope"], audience) or (
                    audience != "@self" and not allows(source["disclosure_scope"], audience)
                ):
                    raise Conflict("原始资料不允许用于当前活动")
                tx.execute(
                    "INSERT OR IGNORE INTO activity_sources VALUES(?,?)", (activity_id, source_id)
                )

    def _assert_context(self, tx, activity_id: str, refs: list[dict] | None = None) -> None:
        if tx.execute(
            "SELECT 1 FROM context_invalidations WHERE activity_id=?", (activity_id,)
        ).fetchone():
            raise ContextInvalidated("当前活动引用的记忆已失效，需要新活动重新构建上下文")
        if tx.execute(
            "SELECT 1 FROM activity_sources a JOIN memory_suppression s "
            "ON a.source_id=s.source_id WHERE a.activity_id=?",
            (activity_id,),
        ).fetchone():
            raise ContextInvalidated("当前活动包含已遗忘的原始资料")
        audience = self._audience(tx, activity_id)
        if refs is None:
            refs = self.references(activity_id)
        for raw in refs:
            ref = MemoryRef.model_validate(raw)
            row = self._row(tx, ref.id)
            if row["revision"] != ref.revision or not self._usable(row, audience):
                raise ContextInvalidated("记忆版本、有效期或受众范围已变化")

    def assert_context(self, activity_id: str, refs: list[dict] | None = None) -> None:
        with self.db.transaction() as tx:
            self._assert_context(tx, activity_id, refs)

    def select_refs(self, activity_id: str, refs: list[dict]) -> None:
        with self.db.transaction() as tx:
            self.lifecycle.check_admission(tx, activity_id)
            self._assert_context(tx, activity_id, refs)
            for ref in refs:
                tx.execute(
                    "INSERT OR IGNORE INTO "
                    "memory_selections(activity_id,memory_id,revision) "
                    "VALUES(?,?,?)",
                    (activity_id, ref["id"], ref["revision"]),
                )

    def invalidate_context(self, activity_id: str, reason: str) -> None:
        with self.db.transaction() as tx:
            self._invalidate_activities(tx, {activity_id}, reason)

    def claim_use(self, activity_id: str, memory_ids: list[str], purpose: str) -> None:
        if not purpose or len(purpose) > 500:
            raise ValueError("使用记忆需要简短用途")
        with self.db.transaction() as tx:
            self.lifecycle.check_admission(tx, activity_id)
            self._assert_context(tx, activity_id)
            for key in set(memory_ids):
                changed = tx.execute(
                    "UPDATE memory_selections SET claimed_purpose=? WHERE activity_id=? "
                    "AND "
                    "memory_id=?",
                    (purpose, activity_id, key),
                ).rowcount
                if not changed:
                    raise Conflict("只能标记本轮实际选入上下文的记忆")

    def commit_uses(self, activity_id: str) -> int:
        settings, now = self.settings(), timestamp(self.clock.now())
        with self.db.transaction() as tx:
            state = tx.execute("SELECT phase,paused FROM characters WHERE id='main'").fetchone()
            if state["phase"] == "sleeping" or state["paused"]:
                return 0
            self._assert_context(tx, activity_id)
            activity = tx.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
            delivered = tx.execute(
                "SELECT 1 FROM deliveries WHERE activity_id=? AND status='sent'", (activity_id,)
            ).fetchone()
            result = json.loads(activity["checkpoint"]).get("agent", {}).get("result")
            if activity["conversation_id"] and not delivered:
                return 0
            if not activity["conversation_id"] and (
                activity["status"] != "completed" or not result
            ):
                return 0
            triggers = []
            for event_id in json.loads(activity["trigger_event_ids"]):
                source = self._source(tx, "event:" + event_id)
                if (
                    source["conversation_id"] == activity["conversation_id"]
                    and source["independent"]
                ):
                    triggers.append(source["id"])
            if not triggers:
                return 0
            applied = 0
            for selection in tx.execute(
                "SELECT * FROM memory_selections WHERE activity_id=? AND claimed_purpose "
                "IS NOT "
                "NULL",
                (activity_id,),
            ).fetchall():
                row = self._row(tx, selection["memory_id"])
                already = tx.execute(
                    "SELECT 1 FROM memory_uses WHERE memory_id=? AND revision=? AND root_id=?",
                    (row["id"], row["revision"], activity["root_id"]),
                ).fetchone()
                if already:
                    continue
                available = [
                    trigger
                    for trigger in triggers
                    if trigger not in row["source_refs"]
                    and not tx.execute(
                        "SELECT 1 FROM memory_use_triggers WHERE memory_id=? AND "
                        "revision=? AND "
                        "trigger_source_id=?",
                        (row["id"], row["revision"], trigger),
                    ).fetchone()
                ]
                if not available:
                    continue
                since = timestamp(self.clock.now() - timedelta(hours=24))
                paid = tx.execute(
                    "SELECT coalesce(sum(reward),0) FROM memory_uses WHERE memory_id=? AND "
                    "used_at>=?",
                    (row["id"], since),
                ).fetchone()[0]
                activation = self._activation(row, settings)
                reward = max(
                    0,
                    min(
                        settings.use_reward,
                        settings.daily_reward_limit - paid,
                        settings.activation_cap - activation,
                    ),
                )
                tx.execute(
                    "INSERT INTO memory_uses VALUES(?,?,?,?,?,?,?)",
                    (
                        row["id"],
                        row["revision"],
                        activity["root_id"],
                        available[0],
                        selection["claimed_purpose"],
                        reward,
                        now,
                    ),
                )
                # 一批输入整体消耗，即使今天已达强化上限，也不能明天重放刷分。
                tx.executemany(
                    "INSERT OR IGNORE INTO memory_use_triggers VALUES(?,?,?,?)",
                    [
                        (row["id"], row["revision"], source, activity["root_id"])
                        for source in triggers
                    ],
                )
                if reward == 0:
                    continue
                until = timestamp(self.clock.now() + timedelta(hours=settings.protected_hours))
                if row["valid_until"]:
                    until = min(until, row["valid_until"])
                tx.execute(
                    "UPDATE memories SET "
                    "activation=?,activation_at=?,last_used_at=?,bucket=CASE "
                    "WHEN tier='recent' THEN 'protected' ELSE bucket "
                    "END,protected_until=? WHERE "
                    "id=?",
                    (activation + reward, now, now, until, row["id"]),
                )
                applied += 1
            self._maintain(tx)
            audit(tx, "memory.use_committed", activity_id, {"rewards": applied}, now)
            return applied
