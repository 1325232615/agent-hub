# -*- coding: utf-8 -*-
"""SQLite 权威库：建表与连接。"""
import sqlite3

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    name         TEXT PRIMARY KEY,
    role         TEXT,
    status       TEXT NOT NULL DEFAULT 'not_started',
    current_task TEXT,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL UNIQUE,
    agent_name  TEXT NOT NULL,
    title       TEXT,
    kind        TEXT NOT NULL DEFAULT 'work',      -- work | milestone
    payload     TEXT,                              -- JSON
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending|claimed|running|done|failed|blocked
    priority    TEXT NOT NULL DEFAULT 'medium',
    depends_on  TEXT,
    result      TEXT,                              -- JSON
    created_at  TEXT,
    updated_at  TEXT,
    claimed_at  TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item       TEXT,
    decision   TEXT,
    decided_by TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_gate (
    gate_key   TEXT PRIMARY KEY,
    decision   TEXT NOT NULL DEFAULT 'NOT_PASS',
    reason     TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT,
    actor       TEXT,
    action      TEXT,
    ref_task_id TEXT,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value_json TEXT,
    updated_at TEXT
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """幂等迁移：老库补列，不破坏既有数据。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(events)")}
    if "target_agent" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN target_agent TEXT")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_db() -> str:
    conn = get_conn()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.execute(
        "INSERT OR IGNORE INTO audit_gate (gate_key, decision, reason, updated_at) "
        "VALUES ('audit_gate','NOT_PASS','尚未审计', datetime('now','localtime'))"
    )
    conn.commit()
    conn.close()
    return DB_PATH


if __name__ == "__main__":
    print("DB initialized:", init_db())
