# -*- coding: utf-8 -*-
"""桥接层：把 SQLite（权威源）单向导出为 quant_hub 只读文件镜像。

导出对象（只写不读，文件侧不再手工编辑）：
  quant_hub/state/project_state.json
  quant_hub/state/audit_gate.json
  quant_hub/agents/<name>/output.json
"""
import json
import os
import shutil
from datetime import datetime

from app.config import QUANT_HUB
from app.db import get_conn

STATE_DIR = os.path.join(QUANT_HUB, "state")
AGENTS_DIR = os.path.join(QUANT_HUB, "agents")
BACKUP_DIR = os.path.join(STATE_DIR, "_bridge_backup")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _loads(s, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def _backup_once(path: str):
    """首次导出前，把原文件备份到 state/_bridge_backup/（只备份一次，不覆盖）。"""
    if not os.path.exists(path):
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    # 用"父目录名__文件名"避免多 Agent 的 output.json 互相覆盖
    parent = os.path.basename(os.path.dirname(path))
    name = f"{parent}__{os.path.basename(path)}"
    dst = os.path.join(BACKUP_DIR, name)
    if not os.path.exists(dst):
        shutil.copy2(path, dst)


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _backup_once(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def export_all() -> dict:
    conn = get_conn()
    try:
        agents = [dict(r) for r in conn.execute("SELECT * FROM agents ORDER BY name")]
        tasks = [dict(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")]
        decisions = [dict(r) for r in conn.execute("SELECT * FROM decisions ORDER BY id")]
        kv = {r["key"]: _loads(r["value_json"]) for r in conn.execute("SELECT * FROM kv")}
        gate_row = conn.execute(
            "SELECT * FROM audit_gate WHERE gate_key='audit_gate'"
        ).fetchone()
        gate = dict(gate_row) if gate_row else {"gate_key": "audit_gate", "decision": "NOT_PASS", "reason": "尚未审计"}
    finally:
        conn.close()

    # 1) project_state.json
    project_state = {
        "project": os.environ.get("HUB_PROJECT", "AgentHub"),
        "updated_at": _now(),
        "source": "mcp_dispatcher/hub.db (SQLite 权威源, 自动生成只读镜像)",
        "agents": {a["name"]: a["status"] for a in agents},
        "agent_detail": {a["name"]: {"role": a["role"], "current_task": a["current_task"], "updated_at": a["updated_at"]} for a in agents},
        "active_tasks": {a["name"]: a["current_task"] for a in agents if a["current_task"]},
        "architecture_direction": kv.get("architecture_direction"),
        "roadmap": [
            {
                "task_id": t["task_id"],
                "task": t["title"],
                "owner": t["agent_name"],
                "status": t["status"],
                "depends_on": t["depends_on"],
            }
            for t in tasks if t["kind"] == "milestone"
        ],
        "decisions_log": [
            {"date": (d["created_at"] or "")[:10], "item": d["item"], "decision": d["decision"], "by": d["decided_by"]}
            for d in decisions
        ],
        "ea_lineage": kv.get("ea_lineage"),
        "pending_buckets": kv.get("pending_buckets"),
        "audit_gate": {"decision": gate["decision"], "reason": gate["reason"], "updated_at": gate["updated_at"]},
    }
    _write_json(os.path.join(STATE_DIR, "project_state.json"), project_state)

    # 2) state/audit_gate.json
    _write_json(os.path.join(STATE_DIR, "audit_gate.json"),
                {"decision": gate["decision"], "reason": gate["reason"]})

    # 3) agents/<name>/output.json
    for a in agents:
        my_tasks = [t for t in tasks if t["agent_name"] == a["name"]]
        output = {
            "agent": a["name"],
            "status": a["status"],
            "role": a["role"],
            "current_task": a["current_task"],
            "updated_at": a["updated_at"],
            "source": "hub.db (自动生成只读镜像)",
            "tasks": [
                {
                    "task_id": t["task_id"],
                    "title": t["title"],
                    "status": t["status"],
                    "result": _loads(t["result"]),
                    "updated_at": t["updated_at"],
                }
                for t in my_tasks
            ],
        }
        _write_json(os.path.join(AGENTS_DIR, a["name"], "output.json"), output)

    return {
        "exported_at": _now(),
        "agents": len(agents),
        "tasks": len(tasks),
        "files": ["state/project_state.json", "state/audit_gate.json"] + [f"agents/{a['name']}/output.json" for a in agents],
    }
