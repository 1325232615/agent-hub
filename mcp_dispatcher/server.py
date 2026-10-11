# -*- coding: utf-8 -*-
"""QuantHub 调度中枢 · MCP 服务（stdio）——本文件是"代理"层。

架构：Trae(MCP stdio) → 本文件 → HTTP → FastAPI(127.0.0.1:8765) → SQLite(hub.db)
说明：本文件不再直接读写 SQLite；所有状态由 FastAPI 统一落库。
原 3 个工具名（dispatch_task / get_my_tasks / submit_result）保持不变，向下兼容。
"""
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

import httpx
from mcp.server.fastmcp import FastMCP

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from app.config import API_BASE, get_token  # noqa: E402

mcp = FastMCP("QuantHub-Dispatcher")

TIMEOUT = 15.0
_tried_start = False


def _headers() -> Dict[str, str]:
    return {"X-Hub-Token": get_token()}


def _raw(method: str, path: str, **kwargs) -> Any:
    url = API_BASE.rstrip("/") + path
    try:
        # trust_env=False：本机系统代理会把 127.0.0.1 的请求打成 502，必须绕过
        with httpx.Client(timeout=TIMEOUT, trust_env=False) as client:
            resp = client.request(method, url, headers=_headers(), **kwargs)
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            return {"ok": False, "http_status": resp.status_code, "error": detail}
        return resp.json()
    except httpx.ConnectError:
        return {"ok": False, "_offline": True, "error": f"无法连接调度中枢 API({API_BASE})"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _start_api() -> bool:
    """API 未启动时，自动以分离进程拉起 uvicorn，并等待健康检查通过。"""
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP（Windows）
    try:
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.api:app", "--host", "127.0.0.1", "--port", "8765"],
            cwd=BASE_DIR, creationflags=flags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001
        return False
    for _ in range(24):
        time.sleep(0.5)
        if _raw("GET", "/health").get("ok"):
            return True
    return False


def _call(method: str, path: str, **kwargs) -> Any:
    """统一 HTTP 调用；离线时自动拉起 API 后重试一次。"""
    global _tried_start
    r = _raw(method, path, **kwargs)
    if isinstance(r, dict) and r.get("_offline") and not _tried_start:
        _tried_start = True
        if _start_api():
            r = _raw(method, path, **kwargs)
        else:
            return {"ok": False, "error": f"调度中枢 API 未启动且自动拉起失败，请手动运行 run_api.bat"}
    if isinstance(r, dict):
        r.pop("_offline", None)
    return r


# ==================== 兼容原有 3 工具 ====================
@mcp.tool()
def dispatch_task(agent_name: str, task_id: str, payload: Dict[str, Any]) -> str:
    """总控专用：向指定 Agent 派发任务。"""
    body = {"task_id": task_id, "agent_name": agent_name, "title": payload.get("title") if isinstance(payload, dict) else None,
            "payload": payload if isinstance(payload, dict) else {"raw": payload}}
    r = _call("POST", "/tasks", json=body)
    if r.get("ok"):
        return f"✅ 任务 {task_id} 已派发给 {agent_name}"
    return f"❌ 派发失败：{r.get('error')}"


@mcp.tool()
def get_my_tasks(agent_name: str) -> List[Dict[str, Any]]:
    """子任务 Agent 专用：获取分配给自己的所有 pending 任务。"""
    r = _call("GET", "/tasks", params={"agent": agent_name, "status": "pending"})
    return r if isinstance(r, list) else [{"error": r}]


@mcp.tool()
def submit_result(task_id: str, result: Dict[str, Any]) -> str:
    """子任务 Agent 专用：提交任务执行结果（状态置 done）。"""
    r = _call("POST", f"/tasks/{task_id}/result", json={"result": result, "status": "done"})
    if r.get("ok"):
        return f"✅ 任务 {task_id} 结果已提交"
    return f"❌ 提交失败：{r.get('error')}"


# ==================== 新增工具 ====================
@mcp.tool()
def claim_task(task_id: str) -> str:
    """子任务 Agent 专用：认领任务（pending → claimed），避免多 Agent 抢单。"""
    r = _call("POST", f"/tasks/{task_id}/claim")
    if r.get("ok"):
        return f"✅ 任务 {task_id} 已认领"
    return f"❌ 认领失败：{r.get('error')}"


@mcp.tool()
def list_tasks(agent_name: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """总控专用：按 Agent / 状态筛选任务列表。"""
    params = {}
    if agent_name:
        params["agent"] = agent_name
    if status:
        params["status"] = status
    r = _call("GET", "/tasks", params=params)
    return r if isinstance(r, list) else [{"error": r}]


@mcp.tool()
def get_project_snapshot() -> Dict[str, Any]:
    """总控专用：一次性拉取全项目快照（各 Agent 状态 + 任务 + 审计关口）。"""
    r = _call("GET", "/snapshot")
    return r if isinstance(r, dict) else {"error": r}


@mcp.tool()
def get_agent_status(agent_name: Optional[str] = None) -> Any:
    """查询单个或全部 Agent 的状态。"""
    if agent_name:
        return _call("GET", f"/agents/{agent_name}")
    return _call("GET", "/agents")


@mcp.tool()
def set_audit_gate(decision: str, reason: str = "") -> str:
    """审计关口专用：设置 audit_gate（PASS / NOT_PASS）。非 PASS 时禁止派发 mt5_gate_bridge。"""
    r = _call("PUT", "/audit_gate", json={"decision": decision, "reason": reason})
    if r.get("ok"):
        return f"✅ 审计关口已置为 {decision}"
    return f"❌ 设置失败：{r.get('error')}"


@mcp.tool()
def get_audit_gate() -> Dict[str, Any]:
    """查询当前审计关口状态。"""
    r = _call("GET", "/audit_gate")
    return r if isinstance(r, dict) else {"error": r}


@mcp.tool()
def log_decision(item: str, decision: str, decided_by: str = "总控") -> str:
    """总控专用：登记一条裁定/决策记录。"""
    r = _call("POST", "/decisions", json={"item": item, "decision": decision, "decided_by": decided_by})
    if r.get("ok"):
        return f"✅ 已登记裁定：{item}"
    return f"❌ 登记失败：{r.get('error')}"


@mcp.tool()
def update_agent_status(agent_name: str, status: Optional[str] = None, current_task: Optional[str] = None) -> str:
    """总控专用：更新某 Agent 的状态 / 当前任务。"""
    body = {}
    if status:
        body["status"] = status
    if current_task is not None:
        body["current_task"] = current_task
    r = _call("PUT", f"/agents/{agent_name}", json=body)
    if r.get("ok") or r.get("name"):
        return f"✅ {agent_name} 状态已更新"
    return f"❌ 更新失败：{r.get('error')}"


@mcp.tool()
def sync_files() -> Dict[str, Any]:
    """总控专用：把 SQLite(权威源) 导出为 quant_hub 只读文件镜像（桥接）。"""
    r = _call("POST", "/bridge/sync")
    return r if isinstance(r, dict) else {"error": r}


if __name__ == "__main__":
    mcp.run(transport="stdio")
