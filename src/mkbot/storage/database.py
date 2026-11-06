"""单实例 SQLite 数据库；事务中禁止进行模型或网络等待。"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any

from mkbot.domain.errors import UnsupportedSchema

SCHEMA_VERSION = 5

SCHEMA = """
CREATE TABLE characters (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, self_id TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'awake', paused INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1, sleep_plan TEXT, wake_at TEXT,
    health TEXT NOT NULL DEFAULT 'healthy', updated_at TEXT NOT NULL
);
CREATE TABLE events (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL, sender_id TEXT NOT NULL, kind TEXT NOT NULL,
    payload TEXT NOT NULL, occurred_at TEXT NOT NULL, received_at TEXT NOT NULL,
    UNIQUE(source, external_id)
);
CREATE TABLE inbox (
    event_id TEXT PRIMARY KEY REFERENCES events(id), status TEXT NOT NULL DEFAULT 'pending',
    decision TEXT, reason TEXT, reviewed_at TEXT, revisit_at TEXT
);
CREATE TABLE activities (
    id TEXT PRIMARY KEY, root_id TEXT NOT NULL, parent_id TEXT REFERENCES activities(id),
    kind TEXT NOT NULL, goal TEXT NOT NULL, origin TEXT NOT NULL,
    conversation_id TEXT, status TEXT NOT NULL DEFAULT 'queued', priority INTEGER NOT NULL,
    due_at TEXT, checkpoint TEXT NOT NULL DEFAULT '{}', version INTEGER NOT NULL DEFAULT 1,
    steps INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE work (
    id TEXT PRIMARY KEY, activity_id TEXT REFERENCES activities(id), kind TEXT NOT NULL,
    status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE deliveries (
    id TEXT PRIMARY KEY, action_key TEXT NOT NULL UNIQUE,
    activity_id TEXT REFERENCES activities(id), conversation_id TEXT NOT NULL,
    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
    work_id TEXT REFERENCES work(id), receipt TEXT, error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
    entity_id TEXT NOT NULL, data TEXT NOT NULL, occurred_at TEXT NOT NULL
);
CREATE TABLE config_revisions (
    name TEXT NOT NULL, revision INTEGER NOT NULL, content TEXT NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY(name, revision)
);
CREATE TABLE budget_limits (
    scope TEXT NOT NULL, metric TEXT NOT NULL, limit_value INTEGER NOT NULL CHECK(limit_value>=0),
    PRIMARY KEY(scope, metric)
);
CREATE TABLE reservations (
    id TEXT NOT NULL, scope TEXT NOT NULL, metric TEXT NOT NULL,
    reserved INTEGER NOT NULL CHECK(reserved>=0), actual INTEGER CHECK(actual>=0),
    state TEXT NOT NULL DEFAULT 'reserved', created_at TEXT NOT NULL,
    PRIMARY KEY(id, scope, metric),
    FOREIGN KEY(scope,metric) REFERENCES budget_limits(scope,metric)
);
CREATE INDEX inbox_status ON inbox(status,revisit_at);
CREATE INDEX activity_due ON activities(status,due_at,priority);
CREATE INDEX work_status ON work(status);
CREATE INDEX delivery_status ON deliveries(status,created_at);
CREATE INDEX event_conversation ON events(conversation_id,received_at);
"""

EXECUTION_SCHEMA = """
CREATE TABLE model_calls (
    id TEXT PRIMARY KEY, activity_id TEXT NOT NULL REFERENCES activities(id),
    request_key TEXT NOT NULL, fingerprint TEXT NOT NULL, role TEXT NOT NULL,
    route_revision INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
    attempt INTEGER NOT NULL, work_id TEXT NOT NULL REFERENCES work(id),
    status TEXT NOT NULL, error_kind TEXT, response TEXT, usage TEXT, prices TEXT NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT, elapsed_ms INTEGER, retry_at TEXT,
    UNIQUE(activity_id,request_key,attempt)
);
CREATE TABLE model_circuits (
    provider TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0, open_until TEXT
);
CREATE TABLE tool_calls (
    id TEXT PRIMARY KEY, activity_id TEXT NOT NULL REFERENCES activities(id),
    invocation_key TEXT NOT NULL, fingerprint TEXT NOT NULL, tool TEXT NOT NULL,
    work_id TEXT NOT NULL REFERENCES work(id), status TEXT NOT NULL,
    result TEXT, started_at TEXT NOT NULL, finished_at TEXT,
    UNIQUE(activity_id,invocation_key)
);
CREATE TABLE plugin_values (
    plugin TEXT NOT NULL, scope TEXT NOT NULL, key TEXT NOT NULL,
    value TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(plugin,scope,key)
);
CREATE INDEX model_activity ON model_calls(activity_id,request_key,attempt);
CREATE INDEX tool_activity ON tool_calls(activity_id,invocation_key);
"""

AGENT_SCHEMA = """
CREATE TABLE run_guards (
    root_id TEXT PRIMARY KEY REFERENCES activities(id), limits TEXT NOT NULL,
    deadline_at TEXT NOT NULL, started_at TEXT NOT NULL
);
CREATE TABLE run_steps (
    root_id TEXT NOT NULL REFERENCES activities(id), step_key TEXT NOT NULL,
    PRIMARY KEY(root_id,step_key)
);
CREATE TABLE run_progress (
    root_id TEXT NOT NULL REFERENCES activities(id), fingerprint TEXT NOT NULL,
    occurrences INTEGER NOT NULL, PRIMARY KEY(root_id,fingerprint)
);
"""

MEMORY_SCHEMA = """
ALTER TABLE activities ADD COLUMN trigger_event_ids TEXT NOT NULL DEFAULT '[]';
ALTER TABLE deliveries ADD COLUMN memory_refs TEXT NOT NULL DEFAULT '[]';
ALTER TABLE tool_calls ADD COLUMN evidence_kind TEXT NOT NULL DEFAULT 'none';
CREATE TABLE memory_sources (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, actor_id TEXT NOT NULL,
    conversation_id TEXT, use_scope TEXT NOT NULL, disclosure_scope TEXT NOT NULL,
    occurred_at TEXT NOT NULL, independent INTEGER NOT NULL, content TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE memories (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, subjects TEXT NOT NULL, content TEXT NOT NULL,
    tier TEXT NOT NULL, bucket TEXT NOT NULL DEFAULT 'fifo', origin_scope TEXT NOT NULL,
    use_scope TEXT NOT NULL, disclosure_scope TEXT NOT NULL, confidence TEXT NOT NULL,
    importance REAL NOT NULL, occurred_at TEXT NOT NULL, valid_from TEXT NOT NULL,
    valid_until TEXT, status TEXT NOT NULL DEFAULT 'active', revision INTEGER NOT NULL DEFAULT 1,
    activation REAL NOT NULL DEFAULT 0, activation_at TEXT NOT NULL,
    last_used_at TEXT, protected_until TEXT, superseded_by TEXT REFERENCES memories(id),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE memory_evidence (
    memory_id TEXT NOT NULL REFERENCES memories(id),
    source_id TEXT NOT NULL REFERENCES memory_sources(id),
    PRIMARY KEY(memory_id,source_id)
);
CREATE TABLE memory_derivations (
    parent_id TEXT NOT NULL REFERENCES memories(id), child_id TEXT NOT NULL REFERENCES memories(id),
    PRIMARY KEY(parent_id,child_id)
);
CREATE TABLE memory_versions (
    memory_id TEXT NOT NULL REFERENCES memories(id), revision INTEGER NOT NULL,
    snapshot TEXT NOT NULL, changed_at TEXT NOT NULL, PRIMARY KEY(memory_id,revision)
);
CREATE TABLE memory_selections (
    activity_id TEXT NOT NULL REFERENCES activities(id),
    memory_id TEXT NOT NULL REFERENCES memories(id),
    revision INTEGER NOT NULL, claimed_purpose TEXT,
    PRIMARY KEY(activity_id,memory_id,revision)
);
CREATE TABLE memory_uses (
    memory_id TEXT NOT NULL REFERENCES memories(id), revision INTEGER NOT NULL,
    root_id TEXT NOT NULL REFERENCES activities(id), trigger_source_id TEXT NOT NULL,
    purpose TEXT NOT NULL, reward REAL NOT NULL, used_at TEXT NOT NULL,
    PRIMARY KEY(memory_id,revision,root_id), UNIQUE(memory_id,revision,trigger_source_id)
);
CREATE TABLE memory_use_triggers (
    memory_id TEXT NOT NULL REFERENCES memories(id), revision INTEGER NOT NULL,
    trigger_source_id TEXT NOT NULL REFERENCES memory_sources(id),
    root_id TEXT NOT NULL REFERENCES activities(id),
    PRIMARY KEY(memory_id,revision,trigger_source_id)
);
CREATE TABLE memory_suppression (
    source_id TEXT PRIMARY KEY REFERENCES memory_sources(id), forgotten_at TEXT NOT NULL
);
CREATE TABLE context_invalidations (
    activity_id TEXT PRIMARY KEY REFERENCES activities(id),
    reason TEXT NOT NULL, occurred_at TEXT NOT NULL
);
CREATE TABLE activity_sources (
    activity_id TEXT NOT NULL REFERENCES activities(id),
    source_id TEXT NOT NULL REFERENCES memory_sources(id),
    PRIMARY KEY(activity_id,source_id)
);
CREATE TABLE plugin_source_refs (
    plugin TEXT NOT NULL, scope TEXT NOT NULL, key TEXT NOT NULL,
    source_id TEXT NOT NULL REFERENCES memory_sources(id),
    PRIMARY KEY(plugin,scope,key,source_id)
);
CREATE TABLE plugin_memory_refs (
    plugin TEXT NOT NULL, scope TEXT NOT NULL, key TEXT NOT NULL,
    memory_id TEXT NOT NULL REFERENCES memories(id), revision INTEGER NOT NULL,
    PRIMARY KEY(plugin,scope,key,memory_id)
);
CREATE INDEX memory_lookup ON memories(status,tier,origin_scope,created_at);
CREATE INDEX memory_root_source ON memory_evidence(source_id,memory_id);
CREATE INDEX activity_source ON activity_sources(source_id,activity_id);
"""


SCHEDULER_SCHEMA = """
ALTER TABLE inbox ADD COLUMN review_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE run_guards ADD COLUMN paused_at TEXT;
CREATE TABLE scheduler_state (
    id TEXT PRIMARY KEY, next_idle_at TEXT NOT NULL, next_dispatch_at TEXT,
    idle_day TEXT NOT NULL, idle_started INTEGER NOT NULL DEFAULT 0,
    chat_burst INTEGER NOT NULL DEFAULT 0, wake_cutoff INTEGER,
    last_reason TEXT NOT NULL DEFAULT '尚未启用调度', updated_at TEXT NOT NULL
);
CREATE TABLE scheduler_jobs (
    activity_id TEXT PRIMARY KEY REFERENCES activities(id), lane TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready', reason TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE inbox_leases (
    event_id TEXT PRIMARY KEY REFERENCES events(id),
    activity_id TEXT NOT NULL REFERENCES activities(id)
);
CREATE TABLE activity_decisions (
    activity_id TEXT PRIMARY KEY REFERENCES activities(id), kind TEXT NOT NULL,
    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL
);
CREATE INDEX scheduler_queue ON scheduler_jobs(status,lane,created_at);
CREATE INDEX inbox_lease_activity ON inbox_leases(activity_id);
"""


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.execute("PRAGMA synchronous=FULL")
        self._migrate()

    def _migrate(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            self.connection.close()
            raise UnsupportedSchema(f"数据库版本 {version} 高于程序支持的 {SCHEMA_VERSION}")
        if version == 0:
            self.connection.executescript(
                "BEGIN IMMEDIATE;\n" + SCHEMA + "\nPRAGMA user_version=1;\nCOMMIT;"
            )
            version = 1
        if version == 1:
            self.connection.executescript(
                "BEGIN IMMEDIATE;\n" + EXECUTION_SCHEMA + "\nPRAGMA user_version=2;\nCOMMIT;"
            )
            version = 2
        if version == 2:
            self.connection.executescript(
                "BEGIN IMMEDIATE;\n" + AGENT_SCHEMA + "\nPRAGMA user_version=3;\nCOMMIT;"
            )
            version = 3
        if version == 3:
            self.connection.executescript(
                "BEGIN IMMEDIATE;\n" + MEMORY_SCHEMA + "\nPRAGMA user_version=4;\nCOMMIT;"
            )
            version = 4
        if version == 4:
            self.connection.executescript(
                "BEGIN IMMEDIATE;\n" + SCHEDULER_SCHEMA + "\nPRAGMA user_version=5;\nCOMMIT;"
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def rows(self, sql: str, parameters: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self.connection.execute(sql, parameters).fetchall()]

    def close(self) -> None:
        with self._lock:
            self.connection.close()
