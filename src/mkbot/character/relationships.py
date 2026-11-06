"""实际互动产生有限关系变化，人物身份、关系判断和管理权限彼此独立。"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from mkbot.domain.errors import AdmissionDenied, Conflict, ContextInvalidated, NotFound
from mkbot.domain.models import StrictModel
from mkbot.memory.contracts import MemoryProposal, valid_scope
from mkbot.runtime.clock import timestamp
from mkbot.runtime.lifecycle import audit
from mkbot.storage.database import encode

if TYPE_CHECKING:
    from mkbot.runtime.kernel import Kernel

AXES = ("familiarity", "affinity", "trust")


def person_id(source: str, sender: str) -> str:
    return source + "/" + sender


def valid_person(value: str) -> str:
    namespace, separator, identifier = value.partition("/")
    if not separator or not namespace or not identifier or len(value) > 250:
        raise ValueError("人物 ID 需要使用平台命名空间/用户 ID")
    return value


class RelationshipSettings(StrictModel):
    automatic_updates: bool = True
    familiarity_step: float = Field(default=2, ge=0, le=10)
    affinity_step: float = Field(default=5, ge=0, le=20)
    trust_step: float = Field(default=5, ge=0, le=20)
    familiarity_daily: float = Field(default=5, ge=0, le=100)
    affinity_daily: float = Field(default=10, ge=0, le=100)
    trust_daily: float = Field(default=10, ge=0, le=100)
    attention_bonus: float = Field(default=10, ge=0, le=15)
    context_people: int = Field(default=6, ge=1, le=20)
    profile_items: int = Field(default=5, ge=0, le=20)
    context_chars: int = Field(default=8000, ge=1000, le=30000)


class RelationshipChange(StrictModel):
    subject_id: str
    reason: str = Field(min_length=1, max_length=1500)
    familiarity: float = Field(default=0, ge=0, le=10)
    affinity: float = Field(default=0, ge=-20, le=20)
    trust: float = Field(default=0, ge=-20, le=20)

    _person = field_validator("subject_id")(valid_person)

    @model_validator(mode="after")
    def meaningful(self):
        if not self.reason.strip() or not any(getattr(self, key) for key in AXES):
            raise ValueError("有实际关系变化时才记录，并提供本次互动的理由")
        return self


class RelationshipEdit(StrictModel):
    expected_version: int = Field(ge=0)
    label: str = Field(default="", max_length=120)
    familiarity: float = Field(ge=0, le=100)
    affinity: float = Field(ge=-100, le=100)
    trust: float = Field(ge=-100, le=100)
    reason: str = Field(min_length=1, max_length=1500)
    scope: list[str] = Field(default_factory=lambda: ["@self"])

    _scope = field_validator("scope")(valid_scope)


class RelationshipService:
    def __init__(self, kernel: Kernel) -> None:
        self.k, self.db, self.clock = kernel, kernel.db, kernel.clock

    def settings(self) -> RelationshipSettings:
        return RelationshipSettings.model_validate(self.k.config.get("relationships")["content"])

    def _ensure(self, tx, subject: str) -> None:
        valid_person(subject)
        now = timestamp(self.clock.now())
        tx.execute(
            "INSERT OR IGNORE INTO people(subject_id,created_at,updated_at) VALUES(?,?,?)",
            (subject, now, now),
        )

    def _baseline(self, tx, subject: str) -> str | None:
        row = tx.execute(
            "SELECT id FROM relationship_changes WHERE subject_id=? AND kind='baseline' ORDER "
            "BY rowid DESC LIMIT 1",
            (subject,),
        ).fetchone()
        return row[0] if row else None

    def _view(self, tx, subject: str, audience: str | None) -> dict:
        rows = tx.execute(
            "SELECT r.*,r.rowid AS sequence FROM relationship_changes r WHERE subject_id=? "
            "AND status='applied' ORDER BY r.rowid",
            (subject,),
        ).fetchall()
        cutoff = max((r["sequence"] for r in rows if r["kind"] == "baseline"), default=0)
        scores, evidence = dict.fromkeys(AXES, 0.0), []
        now = timestamp(self.clock.now())
        for row in rows:
            if row["sequence"] < cutoff:
                continue
            memory = self.k.memory._row(tx, row["memory_id"])
            valid = (
                memory["status"] == "active"
                and memory["revision"] == row["memory_revision"]
                and memory["valid_from"] <= now
                and (not memory["valid_until"] or memory["valid_until"] > now)
            )
            if not valid or (audience is not None and not self.k.memory._usable(memory, audience)):
                continue
            for axis in AXES:
                scores[axis] += row[axis]
            evidence.append(memory)
        for axis in AXES:
            scores[axis] = round(
                max(0 if axis == "familiarity" else -100, min(100, scores[axis])), 4
            )
        return {"scores": scores, "evidence": evidence}

    def get(self, subject: str, audience: str | None = None) -> dict:
        with self.db.transaction() as tx:
            row = tx.execute("SELECT * FROM people WHERE subject_id=?", (subject,)).fetchone()
            if not row:
                raise NotFound("人物尚未登记")
            view = self._view(tx, subject, audience)
            changes = []
            now = timestamp(self.clock.now())
            for change in tx.execute(
                "SELECT * FROM relationship_changes WHERE subject_id=? ORDER BY rowid DESC "
                "LIMIT 50",
                (subject,),
            ).fetchall():
                memory = self.k.memory._row(tx, change["memory_id"])
                visible = audience is None or self.k.memory._usable(memory, audience)
                if not visible:
                    continue
                valid = (
                    memory["status"] == "active"
                    and memory["revision"] == change["memory_revision"]
                    and memory["valid_from"] <= now
                    and (not memory["valid_until"] or memory["valid_until"] > now)
                )
                changes.append(
                    dict(change)
                    | {
                        "reason": memory["content"]
                        if valid or change["status"] == "pending"
                        else "依据已失效",
                        "evidence_valid": valid,
                        "contributes": any(item["id"] == memory["id"] for item in view["evidence"]),
                        "use_scope": memory["use_scope"],
                        "requested": json.loads(change["requested"]),
                    }
                )
            return dict(row) | {
                "scores": view["scores"],
                "audience": audience,
                "changes": changes,
                "evidence_count": len(view["evidence"]),
            }

    def list(self, query: str = "", offset: int = 0) -> list[dict]:
        with self.db.transaction() as tx:
            rows = tx.execute(
                "SELECT * FROM people WHERE instr(subject_id,?) OR instr(label,?) ORDER BY "
                "updated_at DESC,subject_id LIMIT 50 OFFSET ?",
                (query, query, offset),
            ).fetchall()
            return [
                dict(row) | {"scores": self._view(tx, row["subject_id"], None)["scores"]}
                for row in rows
            ]

    def edit(self, subject: str, edit: RelationshipEdit) -> dict:
        valid_person(subject)
        if not edit.reason.strip():
            raise ValueError("人工调整需要说明理由")
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            current = tx.execute(
                "SELECT version FROM people WHERE subject_id=?", (subject,)
            ).fetchone()
            if (current[0] if current else 0) != edit.expected_version:
                raise Conflict("人物关系版本已变化，请重新读取")
            self._ensure(tx, subject)
            source = self.k.memory._operator_source(tx, edit.reason, scope=edit.scope)
            memory = self.k.memory._propose(
                tx,
                MemoryProposal(
                    kind="opinion",
                    subjects=[subject, "self"],
                    content=edit.reason,
                    tier="long",
                    source_refs=[source],
                ),
                operator=True,
            )
            tx.execute(
                "INSERT INTO relationship_changes(id,subject_id,kind,memory_id,memory_revision,"
                "status,familiarity,affinity,trust,created_at,applied_at) "
                "VALUES(?,?,'baseline',?,1,'applied',?,?,?,?,?)",
                (
                    str(uuid4()),
                    subject,
                    memory,
                    edit.familiarity,
                    edit.affinity,
                    edit.trust,
                    now,
                    now,
                ),
            )
            affected = {
                r[0]
                for r in tx.execute(
                    "SELECT DISTINCT s.activity_id FROM memory_selections s JOIN "
                    "relationship_changes r ON r.memory_id=s.memory_id WHERE r.subject_id=?",
                    (subject,),
                )
            }
            self.k.memory._invalidate_activities(tx, affected, "人物关系已人工调整")
            tx.execute(
                "UPDATE people SET label=?,version=?,updated_at=? WHERE subject_id=?",
                (edit.label, edit.expected_version + 1, now, subject),
            )
            audit(tx, "relationship.adjusted", subject, {"version": edit.expected_version + 1}, now)
        self.k.notify()
        return self.get(subject)

    def propose(self, activity_id: str, change: RelationshipChange) -> dict:
        settings = self.settings()
        if not settings.automatic_updates:
            raise AdmissionDenied("自动关系评估已关闭")
        for axis in AXES:
            if abs(getattr(change, axis)) > getattr(settings, axis + "_step"):
                raise Conflict("本次关系变化超过单次上限，请保持渐进")
        self.k.assert_context(activity_id)
        now = timestamp(self.clock.now())
        with self.db.transaction() as tx:
            self.k.lifecycle.check_admission(tx, activity_id)
            activity = tx.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
            existing = tx.execute(
                "SELECT id,status FROM relationship_changes WHERE root_id=? AND subject_id=?",
                (activity["root_id"], change.subject_id),
            ).fetchone()
            if existing:
                return dict(existing) | {"duplicate": True}
            triggers = []
            for event_id in json.loads(activity["trigger_event_ids"]):
                event = tx.execute(
                    "SELECT e.*,i.status FROM events e JOIN inbox i ON i.event_id=e.id WHERE "
                    "e.id=?",
                    (event_id,),
                ).fetchone()
                if (
                    event
                    and event["status"] != "ignored"
                    and event["conversation_id"] == activity["conversation_id"]
                    and person_id(event["source"], event["sender_id"]) == change.subject_id
                ):
                    if tx.execute(
                        "SELECT 1 FROM relationship_triggers WHERE subject_id=? AND event_id=?",
                        (change.subject_id, event_id),
                    ).fetchone():
                        raise Conflict("该次互动已经用于关系评估，不能换根活动重复加分")
                    triggers.append(event_id)
            if not triggers:
                raise Conflict("只能根据本轮真实参与者的分配留言评估关系")
            for event_id in triggers:
                source_id = "event:" + event_id
                self.k.memory._source(tx, source_id)
                tx.execute(
                    "INSERT OR IGNORE INTO activity_sources VALUES(?,?)", (activity_id, source_id)
                )
            self._ensure(tx, change.subject_id)
            memory = self.k.memory._propose(
                tx,
                MemoryProposal(
                    kind="opinion",
                    subjects=[change.subject_id, "self"],
                    content=change.reason,
                    tier="long",
                    source_refs=["event:" + key for key in triggers[:50]],
                ),
                activity_id=activity_id,
                pending=True,
            )
            key = str(uuid4())
            tx.execute(
                "INSERT INTO relationship_changes(id,subject_id,kind,memory_id,memory_revision,"
                "activity_id,root_id,status,requested,baseline_id,created_at) "
                "VALUES(?,?,'delta',?,1,?,?,'pending',?,?,?)",
                (
                    key,
                    change.subject_id,
                    memory,
                    activity_id,
                    activity["root_id"],
                    encode({axis: getattr(change, axis) for axis in AXES}),
                    self._baseline(tx, change.subject_id),
                    now,
                ),
            )
            tx.executemany(
                "INSERT INTO relationship_triggers VALUES(?,?,?)",
                [(change.subject_id, event_id, key) for event_id in triggers],
            )
            audit(tx, "relationship.proposed", key, {"subject_id": change.subject_id}, now)
        return {"id": key, "status": "pending"}

    def commit(self, activity_id: str) -> int:
        settings, now = self.settings(), timestamp(self.clock.now())
        with self.db.transaction() as tx:
            phase = tx.execute("SELECT phase,paused FROM characters WHERE id='main'").fetchone()
            if phase["paused"] or phase["phase"] == "sleeping":
                return 0
            activity = tx.execute("SELECT * FROM activities WHERE id=?", (activity_id,)).fetchone()
            proposals = tx.execute(
                "SELECT * FROM relationship_changes WHERE activity_id=? AND status='pending'",
                (activity_id,),
            ).fetchall()
            if not activity or not proposals:
                return 0
            result = json.loads(activity["checkpoint"]).get("agent", {}).get("result")
            invalid = activity["status"] in ("cancelled", "failed")
            ready = activity["status"] == "completed" and result is not None
            try:
                self.k.memory._assert_context(tx, activity_id)
            except ContextInvalidated:
                invalid = True
            if not invalid and ready:
                if result.get("delivery_id"):
                    delivery = tx.execute(
                        "SELECT status FROM deliveries WHERE id=? AND activity_id=?",
                        (result["delivery_id"], activity_id),
                    ).fetchone()
                    if not delivery or delivery[0] in ("failed", "cancelled"):
                        invalid = True
                    elif delivery[0] != "sent":
                        ready = False
            applied = 0
            for row in proposals:
                memory = self.k.memory._row(tx, row["memory_id"])
                discarded = (
                    invalid
                    or not settings.automatic_updates
                    or memory["status"] != "pending"
                    or (memory["valid_until"] and memory["valid_until"] <= now)
                    or self._baseline(tx, row["subject_id"]) != row["baseline_id"]
                )
                if discarded:
                    tx.execute(
                        "UPDATE relationship_changes SET status='discarded' WHERE id=?",
                        (row["id"],),
                    )
                    if memory["status"] == "pending":
                        tx.execute(
                            "UPDATE memories SET "
                            "status='superseded',revision=revision+1,updated_at=? WHERE id=?",
                            (now, memory["id"]),
                        )
                        self.k.memory._version(tx, memory["id"])
                    continue
                if not ready:
                    continue
                requested = json.loads(row["requested"])
                since = timestamp(self.clock.now() - timedelta(hours=24))
                used = tx.execute(
                    "SELECT coalesce(sum(abs(familiarity)),0) AS "
                    "familiarity,coalesce(sum(abs(affinity)),0) AS "
                    "affinity,coalesce(sum(abs(trust)),0) AS trust FROM relationship_changes "
                    "WHERE subject_id=? AND kind='delta' AND status='applied' AND applied_at>=?",
                    (row["subject_id"], since),
                ).fetchone()
                actual = {
                    axis: (1 if requested[axis] >= 0 else -1)
                    * min(
                        abs(requested[axis]),
                        max(0, getattr(settings, axis + "_daily") - used[axis]),
                    )
                    for axis in AXES
                }
                tx.execute(
                    "UPDATE memories SET status='active',revision=revision+1,updated_at=? "
                    "WHERE id=?",
                    (now, memory["id"]),
                )
                self.k.memory._version(tx, memory["id"])
                tx.execute(
                    "UPDATE relationship_changes SET "
                    "status='applied',memory_revision=?,familiarity=?,affinity=?,trust=?,"
                    "applied_at=? WHERE id=?",
                    (memory["revision"] + 1, *(actual[axis] for axis in AXES), now, row["id"]),
                )
                tx.execute(
                    "UPDATE people SET version=version+1,updated_at=? WHERE subject_id=?",
                    (now, row["subject_id"]),
                )
                audit(tx, "relationship.applied", row["id"], actual, now)
                applied += 1
            return applied

    def reconcile(self) -> None:
        for row in self.db.rows(
            "SELECT DISTINCT activity_id FROM relationship_changes WHERE status='pending' AND "
            "activity_id IS NOT NULL"
        ):
            self.commit(row["activity_id"])

    def invalidate_refs(self, tx, memory_ids: set[str]) -> None:
        for key in memory_ids:
            tx.execute(
                "UPDATE people SET version=version+1,updated_at=? WHERE subject_id IN (SELECT "
                "subject_id FROM relationship_changes WHERE memory_id=?)",
                (timestamp(self.clock.now()), key),
            )

    def attention(self, participants: list[dict]) -> float:
        with self.db.transaction() as tx:
            values = []
            for participant in participants:
                scores = self._view(
                    tx, person_id(participant["source"], participant["sender"]), None
                )["scores"]
                values.append((scores["familiarity"] + max(0, scores["affinity"])) / 200)
            return self.settings().attention_bonus * max(values, default=0)

    def context(self, activity_id: str, subjects: list[str]) -> list[dict]:
        settings = self.settings()
        with self.db.transaction() as tx:
            self.k.lifecycle.check_admission(tx, activity_id)
            audience = self.k.memory._audience(tx, activity_id)
            output = []
            remaining = settings.context_chars
            for subject in list(dict.fromkeys(subjects))[: settings.context_people]:
                self._ensure(tx, subject)
                view = self._view(tx, subject, audience)
                evidence = view["evidence"]
                facts = []
                for row in tx.execute(
                    "SELECT id FROM memories WHERE status='active' "
                    "AND (tier='long' OR kind='boundary') AND EXISTS "
                    "(SELECT 1 FROM json_each(subjects) WHERE value=?) AND NOT EXISTS (SELECT "
                    "1 FROM relationship_changes r WHERE r.memory_id=memories.id) ORDER BY "
                    "kind='boundary' DESC,occurred_at DESC,rowid DESC",
                    (subject,),
                ).fetchall():
                    memory = self.k.memory._row(tx, row[0])
                    if (
                        self.k.memory._usable(memory, audience)
                        and len(facts) < settings.profile_items
                    ):
                        facts.append(memory)
                snippets = []
                selected = {memory["id"]: memory for memory in evidence}
                for memory in facts + evidence[-3:]:
                    if remaining <= 0:
                        break
                    item = self.k.memory.context_item(memory)
                    size = min(remaining, 1500)
                    item["content"] = item["content"][:size]
                    remaining -= len(item["content"])
                    snippets.append(item)
                    selected[memory["id"]] = memory
                for memory in selected.values():
                    tx.execute(
                        "INSERT OR IGNORE INTO "
                        "memory_selections(activity_id,memory_id,revision) VALUES(?,?,?)",
                        (activity_id, memory["id"], memory["revision"]),
                    )
                output.append(
                    {
                        "subject_id": subject,
                        "scores": view["scores"],
                        "profile_and_reasons": snippets,
                    }
                )
            return output
