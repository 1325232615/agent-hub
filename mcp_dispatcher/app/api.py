# -*- coding: utf-8 -*-
"""QuantHub 调度中枢 · FastAPI（HTTP 层）。

启动：在 mcp_dispatcher/ 目录下执行
    .venv\\Scripts\\python.exe -m uvicorn app.api:app --host 127.0.0.1 --port 8765
写操作需带请求头 X-Hub-Token（值见 .hub_token 或环境变量 HUB_TOKEN）。
"""
import asyncio
import json
import time
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse

from app import bridge, models
from app.config import ensure_token, get_token
from app.db import get_conn, init_db

app = FastAPI(title="QuantHub Scheduler", version="0.1.0")

init_db()
ensure_token()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _loads(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return s


def _auth(x_hub_token: Optional[str]):
    token = get_token()
    if token and x_hub_token != token:
        raise HTTPException(status_code=401, detail="无效的 X-Hub-Token")


def _log(conn, actor, action, ref_task_id=None, detail=None, target_agent=None):
    conn.execute(
        "INSERT INTO events (ts,actor,action,ref_task_id,detail,target_agent) VALUES (?,?,?,?,?,?)",
        (_now(), actor, action, ref_task_id,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None,
         target_agent),
    )


def _task_agent(conn, task_id: str) -> Optional[str]:
    row = conn.execute("SELECT agent_name FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    return row["agent_name"] if row else None


def _fetch_events(conn, since: int, agent: Optional[str], limit: int, order: str):
    sql = "SELECT * FROM events WHERE id > ?"
    args: list = [since]
    if agent:
        sql += " AND target_agent = ?"
        args.append(agent)
    sql += " ORDER BY id %s LIMIT ?" % ("ASC" if order.lower() == "asc" else "DESC")
    args.append(limit)
    rows = [dict(r) for r in conn.execute(sql, args)]
    for r in rows:
        r["detail"] = _loads(r["detail"])
    return rows


def _max_event_id(conn) -> int:
    return conn.execute("SELECT COALESCE(MAX(id),0) AS m FROM events").fetchone()["m"]


# ---------------- 健康 ----------------
@app.get("/health")
def health():
    return {"ok": True, "ts": _now(), "db": "hub.db"}


# ---------------- Agent ----------------
@app.get("/agents")
def list_agents():
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM agents ORDER BY name")]
    finally:
        conn.close()


@app.get("/agents/{name}")
def get_agent(name: str):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"agent {name} 不存在")
        return dict(row)
    finally:
        conn.close()


@app.put("/agents/{name}")
def upsert_agent(name: str, body: models.AgentUpdate, x_hub_token: Optional[str] = Header(None)):
    _auth(x_hub_token)
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()
        if row:
            conn.execute(
                "UPDATE agents SET status=COALESCE(?,status), role=COALESCE(?,role), "
                "current_task=COALESCE(?,current_task), updated_at=? WHERE name=?",
                (body.status, body.role, body.current_task, _now(), name),
            )
        else:
            conn.execute(
                "INSERT INTO agents (name, role, status, current_task, updated_at) VALUES (?,?,?,?,?)",
                (name, body.role, body.status or "not_started", body.current_task, _now()),
            )
        _log(conn, "api", "upsert_agent", detail={"name": name, "body": body.model_dump()}, target_agent=name)
        conn.commit()
        return dict(conn.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone())
    finally:
        conn.close()


# ---------------- Task ----------------
@app.get("/tasks")
def list_tasks(agent: Optional[str] = None, status: Optional[str] = None):
    conn = get_conn()
    try:
        sql = "SELECT * FROM tasks WHERE 1=1"
        args = []
        if agent:
            sql += " AND agent_name=?"
            args.append(agent)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY id"
        rows = [dict(r) for r in conn.execute(sql, args)]
        for r in rows:
            r["payload"] = _loads(r["payload"])
            r["result"] = _loads(r["result"])
        return rows
    finally:
        conn.close()


@app.post("/tasks")
def create_task(body: models.TaskCreate, x_hub_token: Optional[str] = Header(None)):
    _auth(x_hub_token)
    conn = get_conn()
    try:
        try:
            conn.execute(
                "INSERT INTO tasks (task_id, agent_name, title, kind, payload, status, priority, "
                "depends_on, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (body.task_id, body.agent_name, body.title, body.kind,
                 json.dumps(body.payload, ensure_ascii=False), "pending",
                 body.priority, body.depends_on, _now(), _now()),
            )
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(status_code=409, detail=f"任务 {body.task_id} 已存在")
            raise
        _log(conn, "api", "create_task", body.task_id,
             {"agent": body.agent_name, "title": body.title}, target_agent=body.agent_name)
        conn.commit()
        return {"ok": True, "task_id": body.task_id}
    finally:
        conn.close()


@app.post("/tasks/{task_id}/claim")
def claim_task(task_id: str, x_hub_token: Optional[str] = Header(None)):
    _auth(x_hub_token)
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE tasks SET status='claimed', claimed_at=?, updated_at=? WHERE task_id=? AND status='pending'",
            (_now(), _now(), task_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=409, detail=f"任务 {task_id} 非 pending，无法认领")
        _log(conn, "api", "claim_task", task_id, target_agent=_task_agent(conn, task_id))
        conn.commit()
        return {"ok": True, "task_id": task_id, "status": "claimed"}
    finally:
        conn.close()


@app.post("/tasks/{task_id}/result")
def submit_result(task_id: str, body: models.TaskResult, x_hub_token: Optional[str] = Header(None)):
    _auth(x_hub_token)
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE tasks SET status=?, result=?, finished_at=?, updated_at=? WHERE task_id=? AND status IN ('pending','claimed','running')",
            (body.status, json.dumps(body.result, ensure_ascii=False), _now(), _now(), task_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=409, detail=f"任务 {task_id} 无可提交状态")
        _log(conn, "api", "submit_result", task_id, {"status": body.status},
             target_agent=_task_agent(conn, task_id))
        conn.commit()
        return {"ok": True, "task_id": task_id, "status": body.status}
    finally:
        conn.close()


@app.post("/tasks/{task_id}/status")
def set_task_status(task_id: str, body: models.TaskStatus, x_hub_token: Optional[str] = Header(None)):
    _auth(x_hub_token)
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE tasks SET status=?, updated_at=? WHERE task_id=?",
            (body.status, _now(), task_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
        _log(conn, "api", "set_task_status", task_id, {"status": body.status, "note": body.note},
             target_agent=_task_agent(conn, task_id))
        conn.commit()
        return {"ok": True, "task_id": task_id, "status": body.status}
    finally:
        conn.close()


@app.delete("/tasks/{task_id}")
def delete_task(task_id: str, x_hub_token: Optional[str] = Header(None)):
    """删除一条任务记录（仅删 tasks 行，审计事件保留可追溯）。"""
    _auth(x_hub_token)
    conn = get_conn()
    try:
        row = conn.execute("SELECT agent_name FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
        conn.execute("DELETE FROM tasks WHERE task_id=?", (task_id,))
        _log(conn, "api", "delete_task", task_id, {"agent": row["agent_name"]})
        conn.commit()
        return {"ok": True, "task_id": task_id, "deleted": True}
    finally:
        conn.close()


# ---------------- Decision ----------------
@app.get("/decisions")
def list_decisions():
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM decisions ORDER BY id")]
    finally:
        conn.close()


@app.post("/decisions")
def add_decision(body: models.DecisionCreate, x_hub_token: Optional[str] = Header(None)):
    _auth(x_hub_token)
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO decisions (item, decision, decided_by, created_at) VALUES (?,?,?,?)",
            (body.item, body.decision, body.decided_by, _now()),
        )
        _log(conn, "api", "add_decision", detail={"item": body.item})
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


# ---------------- Audit Gate ----------------
@app.get("/audit_gate")
def get_gate():
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM audit_gate WHERE gate_key='audit_gate'").fetchone()
        return dict(row) if row else {"decision": "NOT_PASS"}
    finally:
        conn.close()


@app.put("/audit_gate")
def set_gate(body: models.GateUpdate, x_hub_token: Optional[str] = Header(None)):
    _auth(x_hub_token)
    if body.decision not in ("PASS", "NOT_PASS"):
        raise HTTPException(status_code=400, detail="decision 只能是 PASS 或 NOT_PASS")
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE audit_gate SET decision=?, reason=?, updated_at=? WHERE gate_key='audit_gate'",
            (body.decision, body.reason, _now()),
        )
        _log(conn, "api", "set_audit_gate", detail={"decision": body.decision})
        conn.commit()
        return {"ok": True, "decision": body.decision}
    finally:
        conn.close()


# ---------------- Events / KV ----------------
@app.get("/events")
def list_events(
    limit: int = Query(100, le=1000),
    since: int = Query(0, ge=0),
    agent: Optional[str] = None,
    order: str = Query("desc", pattern="^(asc|desc)$"),
):
    """事件列表（默认取最新 limit 条，兼容旧调用）。

    since 指定后仅返回 id > since 的事件；agent 指定后仅返回发给该 Agent 的事件。
    """
    conn = get_conn()
    try:
        return _fetch_events(conn, since, agent, limit, order)
    finally:
        conn.close()


@app.get("/events/wait")
def wait_events(
    since: int = Query(0, ge=0),
    agent: Optional[str] = None,
    timeout: float = Query(25, le=120),
    limit: int = Query(200, le=1000),
):
    """长轮询事件通道：挂起至有新事件或超时。

    since=0 表示"从现在开始等"，不回溯历史（要历史请用 GET /events）。
    返回 last_id 供下次续接，保证不丢不重。
    """
    deadline = time.monotonic() + timeout
    cursor = since
    first = True
    while True:
        conn = get_conn()
        try:
            if first and cursor == 0:
                cursor = _max_event_id(conn)
            rows = _fetch_events(conn, cursor, agent, limit, "asc")
        finally:
            conn.close()
        if rows:
            return {"ok": True, "events": rows, "last_id": rows[-1]["id"], "timed_out": False}
        first = False
        if time.monotonic() >= deadline:
            return {"ok": True, "events": [], "last_id": cursor, "timed_out": True}
        time.sleep(0.4)


@app.get("/events/stream")
async def stream_events(
    since: int = Query(0, ge=0),
    agent: Optional[str] = None,
    timeout: float = Query(300, le=3600),
    heartbeat: float = Query(15, le=60),
):
    """SSE 事件流：有新事件即推；空闲时按 heartbeat 发注释保活；到 timeout 正常收尾。"""

    async def gen():
        cursor = since
        deadline = time.monotonic() + timeout
        last_beat = time.monotonic()
        yield "retry: 3000\n\n"
        while True:
            conn = get_conn()
            try:
                if cursor == 0:
                    cursor = _max_event_id(conn)
                rows = _fetch_events(conn, cursor, agent, 200, "asc")
            finally:
                conn.close()
            for r in rows:
                cursor = max(cursor, r["id"])
                yield "id: %d\nevent: hub\ndata: %s\n\n" % (
                    r["id"], json.dumps(r, ensure_ascii=False))
            now = time.monotonic()
            if rows:
                last_beat = now
            elif now - last_beat >= heartbeat:
                last_beat = now
                yield ": ping\n\n"
            if now >= deadline:
                yield 'event: bye\ndata: {"reason":"timeout"}\n\n'
                return
            await asyncio.sleep(0.4)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/kv/{key}")
def get_kv(key: str):
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM kv WHERE key=?", (key,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"kv {key} 不存在")
        return {"key": key, "value": _loads(row["value_json"])}
    finally:
        conn.close()


@app.put("/kv")
def set_kv(body: models.KvSet, x_hub_token: Optional[str] = Header(None)):
    _auth(x_hub_token)
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO kv (key, value_json, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
            (body.key, json.dumps(body.value, ensure_ascii=False), _now()),
        )
        _log(conn, "api", "set_kv", detail={"key": body.key})
        conn.commit()
        return {"ok": True, "key": body.key}
    finally:
        conn.close()


# ---------------- Snapshot / Bridge ----------------
@app.get("/snapshot")
def snapshot():
    conn = get_conn()
    try:
        agents = [dict(r) for r in conn.execute("SELECT * FROM agents ORDER BY name")]
        tasks = [dict(r) for r in conn.execute("SELECT * FROM tasks ORDER BY id")]
        gate = conn.execute("SELECT * FROM audit_gate WHERE gate_key='audit_gate'").fetchone()
        return {
            "ts": _now(),
            "agents": {a["name"]: a["status"] for a in agents},
            "agent_detail": agents,
            "tasks": tasks,
            "audit_gate": dict(gate) if gate else None,
        }
    finally:
        conn.close()


@app.post("/bridge/sync")
def bridge_sync(x_hub_token: Optional[str] = Header(None)):
    _auth(x_hub_token)
    result = bridge.export_all()
    conn = get_conn()
    try:
        _log(conn, "api", "bridge_sync", detail=result)
        conn.commit()
    finally:
        conn.close()
    return result


# ---------------- 只读实时看板 ----------------
# 纯静态页面 + 轮询 /snapshot 与 /events，5 秒自刷新；不写库、不需要 token。
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>QuantHub 看板</title>
<style>
 :root{--bg:#0d1117;--card:#161b22;--bd:#30363d;--tx:#c9d1d9;--mu:#8b949e;
       --ok:#3fb950;--run:#58a6ff;--warn:#d29922;--err:#f85149}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--tx);
      font:13px/1.6 "Segoe UI",system-ui,"Microsoft YaHei",sans-serif}
 header{display:flex;align-items:center;gap:12px;padding:10px 16px;
        border-bottom:1px solid var(--bd);position:sticky;top:0;background:var(--bg);z-index:9}
 h1{font-size:15px;margin:0;font-weight:600}
 h2{font-size:13px;margin:18px 16px 6px;color:var(--mu);font-weight:600}
 .sp{flex:1}.mu{color:var(--mu)}.sm{font-size:12px}
 .mono{font-family:ui-monospace,Consolas,monospace}
 #agents{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:10px;padding:12px 16px}
 .card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:10px 12px}
 .hd{display:flex;align-items:center;gap:8px;margin-bottom:6px}
 .ct{font-size:12px;color:var(--tx);word-break:break-all;max-height:52px;overflow:hidden}
 .tag{padding:1px 8px;border-radius:99px;font-size:11px;border:1px solid currentColor}
 .tag.ok{color:var(--ok)}.tag.run{color:var(--run)}.tag.warn{color:var(--warn)}
 .tag.err{color:var(--err)}.tag.mu{color:var(--mu)}
 table{width:calc(100% - 32px);margin:0 16px;border-collapse:collapse}
 th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--bd);font-size:12px}
 th{color:var(--mu);font-weight:500}
 ul{list-style:none;margin:0 16px;padding:0}
 li{padding:3px 0;border-bottom:1px solid rgba(48,54,61,.4);font-size:12px}
 #err{color:var(--err)}
</style>
</head>
<body>
<header>
  <h1>QuantHub 看板</h1>
  <span class="mu" id="ts">加载中…</span>
  <span class="sp"></span>
  <span id="err"></span>
  <label class="mu"><input type="checkbox" id="pause"> 暂停刷新</label>
</header>
<h2>AGENT 状态</h2>
<section id="agents"></section>
<h2>任务（最近 10 条）</h2>
<table id="tasks"><tbody></tbody></table>
<h2>事件流水（最近 20 条）</h2>
<ul id="events"></ul>
<script>
const $ = s => document.querySelector(s);
function esc(s){return (s==null?'':String(s)).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function cls(st){
  if(st==='done') return 'ok';
  if(st==='failed'||st==='blocked') return 'err';
  if(st==='in_progress'||st==='running') return 'run';
  if(st==='pending'||st==='claimed') return 'warn';
  return 'mu';
}
async function load(){
  try{
    const r = await Promise.all([
      fetch('/snapshot').then(x=>x.json()),
      fetch('/events?limit=20').then(x=>x.json())
    ]);
    const snap = r[0], evs = r[1] || [];
    $('#ts').textContent = '最后刷新 ' + snap.ts;
    const order = ['data_env','strategy_1m','audit','mt5_gate_bridge','strategy_5m'];
    const det = {};
    (snap.agent_detail||[]).forEach(a => det[a.name] = a);
    $('#agents').innerHTML = order.map(function(n){
      const a = det[n] || {name:n, status:'not_started'};
      return '<div class="card"><div class="hd"><b>'+esc(n)+'</b>'
        + '<span class="tag '+cls(a.status)+'">'+esc(a.status)+'</span></div>'
        + '<div class="ct">'+esc(a.current_task||'—')+'</div>'
        + '<div class="mu sm">更新 '+esc(a.updated_at||'—')+'</div></div>';
    }).join('');
    const ts = (snap.tasks||[]).slice().sort(function(x,y){
      return String(y.updated_at||'').localeCompare(String(x.updated_at||''));
    }).slice(0,10);
    $('#tasks tbody').innerHTML = '<tr><th>任务</th><th>Agent</th><th>状态</th><th>耗时</th><th>轮数</th><th>更新时间</th></tr>'
      + ts.map(function(t){
        const r2 = t.result || {};
        return '<tr><td class="mono">'+esc(t.task_id)+'</td><td>'+esc(t.agent_name)+'</td>'
          + '<td><span class="tag '+cls(t.status)+'">'+esc(t.status)+'</span></td>'
          + '<td>'+(r2.elapsed_sec!=null?r2.elapsed_sec+'s':'—')+'</td>'
          + '<td>'+(r2.num_turns!=null?r2.num_turns:'—')+'</td>'
          + '<td class="mu">'+esc(t.updated_at||'')+'</td></tr>';
      }).join('');
    $('#events').innerHTML = evs.map(function(e){
      return '<li><span class="mu">'+esc(e.ts)+'</span> <b>'+esc(e.action)+'</b> '
        + '<span class="mu">'+esc(e.target_agent||'')+'</span> '
        + '<span class="mono mu">'+esc(e.ref_task_id||'')+'</span></li>';
    }).join('');
    $('#err').textContent = '';
  }catch(err){
    $('#err').textContent = '连接失败: ' + err.message;
  }
}
$('#pause').addEventListener('change', function(){ if(!$('#pause').checked) load(); });
load();
setInterval(function(){ if(!$('#pause').checked) load(); }, 5000);
</script>
</body>
</html>
"""


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """只读实时看板：浏览器打开即可，5 秒自动刷新。"""
    return DASHBOARD_HTML
