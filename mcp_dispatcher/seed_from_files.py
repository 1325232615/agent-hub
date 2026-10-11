# -*- coding: utf-8 -*-
"""一次性种子导入：把现有 quant_hub 文件体系（JSON）导入 SQLite 权威库。

读取：
  quant_hub/state/project_state.json
  quant_hub/state/audit_gate.json
  quant_hub/agents/<name>/output.json
  mcp_dispatcher/tasks.db（旧库，若存在则迁移任务行）
写入：mcp_dispatcher/hub.db
注意：只读原文件，不修改、不删除；重复执行幂等（INSERT OR IGNORE / REPLACE）。
"""
import json
import os
import sqlite3
from datetime import datetime

from app.config import BASE_DIR, QUANT_HUB
from app.db import get_conn, init_db

PROJECT_STATE = os.path.join(QUANT_HUB, "state", "project_state.json")
AUDIT_GATE = os.path.join(QUANT_HUB, "state", "audit_gate.json")
AGENTS_DIR = os.path.join(QUANT_HUB, "agents")
OLD_DB = os.path.join(BASE_DIR, "tasks.db")

CANON_AGENTS = ["strategy_1m", "strategy_5m", "data_env", "audit", "mt5_gate_bridge"]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _read_json(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def seed():
    init_db()
    conn = get_conn()
    stats = {"agents": 0, "tasks": 0, "decisions": 0, "kv": 0, "gate": 0, "event": 0}

    ps = _read_json(PROJECT_STATE) or {}
    ag = _read_json(AUDIT_GATE) or {}

    # ---- 1) Agents ----
    names = set(CANON_AGENTS) | set((ps.get("agents") or {}).keys())
    for name in sorted(names):
        status = (ps.get("agents") or {}).get(name, "not_started")
        cur_task = (ps.get("active_tasks") or {}).get(name)
        conn.execute(
            "INSERT OR REPLACE INTO agents (name, role, status, current_task, updated_at) VALUES (?,?,?,?,?)",
            (name, None, status, cur_task, _now()),
        )
        stats["agents"] += 1

    # ---- 2) Roadmap（kind=milestone）----
    for item in (ps.get("roadmap") or []):
        tid = item.get("task_id") or f"roadmap-{item.get('step', '?')}"
        conn.execute(
            "INSERT OR IGNORE INTO tasks (task_id, agent_name, title, kind, payload, status, priority, "
            "depends_on, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tid, item.get("owner") or "总控", item.get("task"), "milestone",
             json.dumps({"step": item.get("step")}, ensure_ascii=False),
             item.get("status", "pending"), "high", item.get("depends_on"), _now(), _now()),
        )
        stats["tasks"] += 1

    # ---- 3) Decisions ----
    for d in (ps.get("decisions_log") or []):
        conn.execute(
            "INSERT INTO decisions (item, decision, decided_by, created_at) VALUES (?,?,?,?)",
            (d.get("item"), d.get("decision"), d.get("by") or d.get("decided_by") or "总控",
             d.get("date") or _now()),
        )
        stats["decisions"] += 1

    # ---- 4) KV（ea_lineage / architecture_direction / pending_buckets）----
    for key in ("ea_lineage", "architecture_direction", "pending_buckets"):
        if ps.get(key) is not None:
            conn.execute(
                "INSERT OR REPLACE INTO kv (key, value_json, updated_at) VALUES (?,?,?)",
                (key, json.dumps(ps[key], ensure_ascii=False), _now()),
            )
            stats["kv"] += 1

    # ---- 5) Audit Gate ----
    decision = ag.get("decision") or (ps.get("audit_gate") or {}).get("decision") or "NOT_PASS"
    reason = ag.get("reason") or (ps.get("audit_gate") or {}).get("reason")
    conn.execute(
        "INSERT OR REPLACE INTO audit_gate (gate_key, decision, reason, updated_at) VALUES ('audit_gate',?,?,?)",
        (decision, reason, _now()),
    )
    stats["gate"] += 1

    # ---- 6) agents/*/output.json（仅取状态，任务明细以 hub 为准）----
    if os.path.isdir(AGENTS_DIR):
        for name in os.listdir(AGENTS_DIR):
            op = os.path.join(AGENTS_DIR, name, "output.json")
            data = _read_json(op)
            if not isinstance(data, dict):
                continue
            if data.get("status"):
                conn.execute("UPDATE agents SET status=? WHERE name=? AND status='not_started'",
                             (data["status"], name))

    conn.commit()

    # ---- 7) 旧库 tasks.db 迁移 ----
    if os.path.exists(OLD_DB):
        try:
            old = sqlite3.connect(OLD_DB)
            old.row_factory = sqlite3.Row
            rows = old.execute("SELECT * FROM tasks").fetchall()
            for r in rows:
                conn.execute(
                    "INSERT OR IGNORE INTO tasks (task_id, agent_name, title, kind, payload, status, priority, "
                    "result, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (r["task_id"], r["agent_name"], None, "work", r["payload"], r["status"], "medium",
                     r["result"], r["created_at"], r["updated_at"]),
                )
                stats["tasks"] += 1
            old.close()
        except Exception as e:  # noqa: BLE001
            print("旧库迁移跳过：", e)

    conn.execute(
        "INSERT INTO events (ts, actor, action, detail) VALUES (?,?,?,?)",
        (_now(), "seed", "seed_from_files", json.dumps(stats, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()
    return stats


if __name__ == "__main__":
    print("种子导入完成：", seed())
