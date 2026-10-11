# -*- coding: utf-8 -*-
"""QuantHub 守护进程：订阅中枢事件 → 唤醒 WorkBuddy(CodeBuddy CLI) → 回执结果。

架构
----
    中枢 FastAPI(/events/wait 长轮询)  ──派单事件──▶  本进程
    本进程 ──cbc -p "<指令>"──▶ WorkBuddy 侧 Agent（无头执行）
    本进程 ──POST /tasks/{id}/result──▶ 中枢（回执）

要点
----
* 用 `--model custom-local:*`（走用户自建 OpenAI 兼容端点），**无需 CodeBuddy 账号登录**。
* 长轮询带 last_id 续接，重启不丢单；启动时额外补偿拉取本 Agent 的 pending 任务。
* HTTP 客户端强制 `trust_env=False`：本机系统代理会把 127.0.0.1 请求打成 502。

用法
----
    .venv\\Scripts\\python.exe hub_daemon.py --agent <agent名> [--once]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from app.config import API_BASE, get_token  # noqa: E402

# ==================== 可调参数 ====================
MODEL = os.environ.get("HUB_DAEMON_MODEL", "custom-local:deepseek-flash")
MAX_TURNS = int(os.environ.get("HUB_DAEMON_MAX_TURNS", "60"))
TASK_TIMEOUT = float(os.environ.get("HUB_DAEMON_TIMEOUT", "1800"))     # 单任务上限（秒）
POLL_TIMEOUT = float(os.environ.get("HUB_DAEMON_POLL_TIMEOUT", "25"))  # 长轮询挂起时长
# 额外 CLI 参数（空格分隔）。需要全程免确认可追加 -y。
EXTRA_ARGS = [a for a in os.environ.get("HUB_DAEMON_CBC_ARGS", "").split(" ") if a]
# cbc 权限模式；留空则不传（沙箱默认会拒绝工作目录外的读写）
PERMISSION_MODE = os.environ.get("HUB_DAEMON_PERM", "")
# 额外授权给 Agent 的目录（分号分隔），用于读取工作目录外的记忆/正源/报告
ADD_DIRS = [d for d in os.environ.get("HUB_DAEMON_ADD_DIRS", "").split(";") if d]
# 输出格式：stream-json 会逐条输出执行事件（含工具调用/思考），用于看板"点进去看思考"
OUTPUT_FORMAT = os.environ.get("HUB_DAEMON_OUTFMT", "stream-json")

STATE_FILE = BASE_DIR / ".daemon_state.json"
LOG_FILE = BASE_DIR / "daemon.log"
TRACE_DIR = BASE_DIR / "daemon_traces"          # 每个任务的完整执行轨迹（NDJSON）
WORKDIR = Path(os.environ.get("HUB_DAEMON_WORKDIR", BASE_DIR / "daemon_workdir"))

HTTP = httpx.Client(trust_env=False, timeout=60.0)
# ==================================================


def log(msg: str) -> None:
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------- 中枢 HTTP ----------------
def _headers() -> dict:
    return {"X-Hub-Token": get_token()}


def api_get(path: str, params: dict | None = None):
    r = HTTP.get(API_BASE.rstrip("/") + path, params=params)
    r.raise_for_status()
    return r.json()


def api_post(path: str, payload=None):
    r = HTTP.post(API_BASE.rstrip("/") + path, json=payload, headers=_headers())
    return r


def api_put(path: str, payload=None):
    r = HTTP.put(API_BASE.rstrip("/") + path, json=payload, headers=_headers())
    return r


# ---------------- WorkBuddy CLI 定位 ----------------
def find_node() -> str | None:
    if os.environ.get("WORKBUDDY_NODE"):
        return os.environ["WORKBUDDY_NODE"]
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))
    versions = home / ".workbuddy" / "binaries" / "node" / "versions"
    found = sorted(versions.glob("*/node.exe")) if versions.is_dir() else []
    if found:
        return str(found[-1])
    return shutil.which("node")


def find_cli() -> str | None:
    if os.environ.get("WORKBUDDY_CLI"):
        return os.environ["WORKBUDDY_CLI"]
    roots = [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "WorkBuddyAI",
             Path("D:/_C盘迁移/WorkBuddy/Programs_WorkBuddyAI")]
    for root in roots:
        p = root / "resources" / "app.asar.unpacked" / "cli" / "bin" / "codebuddy"
        if p.is_file():
            return str(p)
    return None


# ---------------- 任务执行 ----------------
def build_prompt(task: dict) -> str:
    payload = task.get("payload")
    # 对话类任务：payload.prompt 已是装配好的完整提示词（含历史上下文），直通使用
    if isinstance(payload, dict) and payload.get("prompt"):
        return str(payload["prompt"])
    parts = []
    if task.get("title"):
        parts.append("# 任务：%s" % task["title"])
    parts.append("任务ID：%s" % task.get("task_id"))
    if task.get("payload"):
        parts.append("载荷：\n" + json.dumps(task["payload"], ensure_ascii=False, indent=2))
    parts.append("请执行上述任务，给出简明结论；若有产物，列出其绝对路径。")
    parts.append(
        "【★若需澄清/受阻，严禁干等】严禁提问后原地等待。若你需要用户或总控确认任何事、"
        "或任务无法完成，必须【立即】把问题回执到中枢（status 置 blocked、note 写问题原文），命令：\n"
        "  $tok=(Get-Content '%s' -Raw).Trim()\n"
        "  $b=@{status='blocked';note='<你的问题原文>'}|ConvertTo-Json -Compress\n"
        "  Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8765/tasks/%s/status' "
        "-Headers @{'X-Hub-Token'=$tok} -ContentType 'application/json; charset=utf-8' "
        "-Body ([Text.Encoding]::UTF8.GetBytes($b))\n"
        "回执后再输出一句“已上报待确认”，不要自行猜测或假执行。"
        % (str(BASE_DIR / ".hub_token"), task.get("task_id"))
    )
    return "\n\n".join(parts)


def parse_result(text: str) -> dict:
    """从 cbc 输出里取出终态结果对象。

    兼容两种 --output-format：
      * stream-json：NDJSON，每行一个事件；取最后一条 type=="result" 的事件；
      * json：单个 JSON 对象/数组。
    """
    txt = (text or "").strip()
    # 1) stream-json：逐行解析，取最后一条 result 事件
    last_result = None
    for ln in txt.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            ev = json.loads(ln)
        except Exception:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            last_result = ev
    if last_result is not None:
        return last_result
    # 2) 退回单块 JSON（旧格式）
    data = None
    try:
        data = json.loads(txt)
    except Exception:
        i, j = txt.find("["), txt.rfind("]")
        if i >= 0 and j > i:
            try:
                data = json.loads(txt[i:j + 1])
            except Exception:
                data = None
    if isinstance(data, list):
        for item in reversed(data):
            if isinstance(item, dict) and ("result" in item or item.get("type") == "result"):
                return item
    elif isinstance(data, dict):
        return data
    return {"raw_tail": txt[-12000:]}


def save_trace(agent: str, task_id: str, stdout: str, stderr: str) -> str | None:
    """保存完整执行轨迹（stream-json NDJSON 原文 + stderr），供看板"点进去看思考"。

    返回相对 mcp_dispatcher 的路径（POSIX 分隔），失败返回 None。
    """
    try:
        d = TRACE_DIR / (agent or "unknown")
        d.mkdir(parents=True, exist_ok=True)
        p = d / ("%s.jsonl" % task_id)
        body = stdout or ""
        if stderr:
            body += "\n" + json.dumps({"type": "_stderr", "text": stderr[-4000:]}, ensure_ascii=False) + "\n"
        p.write_text(body, encoding="utf-8")
        return str(p.relative_to(BASE_DIR)).replace("\\", "/")
    except Exception as e:  # noqa: BLE001
        log("轨迹保存失败 %s: %s" % (task_id, e))
        return None


def resolve_model(agent: str) -> str:
    """解析该 Agent 当前应使用的模型。

    优先级：中枢 KV 'daemon_model'（一个 {agent: model} 映射）> 环境变量/默认 MODEL。
    这样用户可在看板上随时切换各机器人的模型，**下一次派单即生效**（无需重启进程）。
    """
    try:
        r = api_get("/kv/daemon_model")
        val = r.get("value") if isinstance(r, dict) else None
        if isinstance(val, dict) and val.get(agent):
            return str(val[agent])
    except Exception:  # noqa: BLE001
        pass
    return MODEL


def identity_prefix(agent: str) -> str:
    """构造该 Agent 的身份前缀（代号 + 角色/职责 + 当前任务背景）。

    唤醒的是通用 CLI（默认自称 CodeBuddy Code），必须显式告诉它"你是谁"，
    否则被问"你是不是数据侧/审计侧/M5 线"时会否定自己的身份。
    """
    role = cur = ""
    try:
        r = api_get("/agents/%s" % agent)
        role = (r.get("role") or "").strip()
        cur = (r.get("current_task") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    s = "你是 QuantHub 调度中枢下属的专职机器人，代号「%s」。" % agent
    if role:
        s += "你的角色与职责：%s。" % role
    if cur:
        s += "当前任务背景：%s。" % cur
    s += ("你是这个岗位的专职机器人，不是通用助手，不得自称通用 AI 编码工具；"
          "请始终以该身份与职责作答。当被问及你的身份（例如『你是不是数据侧 / 审计侧 / M5 线』）时，"
          "应依据上面的角色如实确认。")
    return s


def run_agent(agent: str, task_id: str, prompt: str) -> tuple[bool, dict]:
    node, cli = find_node(), find_cli()
    if not node or not cli:
        return False, {"error": "找不到 WorkBuddy node/CLI", "node": node, "cli": cli}

    model = resolve_model(agent)
    full_prompt = identity_prefix(agent) + "\n\n" + prompt   # 注入身份，让机器人认得自己
    WORKDIR.mkdir(parents=True, exist_ok=True)
    cmd = [node, cli, "-p", full_prompt, "--model", model,
           "--output-format", OUTPUT_FORMAT, "--max-turns", str(MAX_TURNS)]
    if OUTPUT_FORMAT == "stream-json":
        cmd += ["--verbose"]
    if PERMISSION_MODE:
        cmd += ["--permission-mode", PERMISSION_MODE]
    for d in ADD_DIRS:
        cmd += ["--add-dir", d]
    cmd += EXTRA_ARGS
    env = dict(os.environ)
    env["CODEBUDDY_SKIP_GIT_BASH_CHECK"] = "1"

    # 实时轨迹：先写 meta，之后 stdout 每来一行就 flush 一次 —— 看板可边跑边看
    live_dir = TRACE_DIR / (agent or "unknown")
    live_dir.mkdir(parents=True, exist_ok=True)
    live_path = live_dir / ("%s.jsonl" % task_id)
    t0 = time.time()
    out_lines: list[str] = []
    err_box: list[str] = []
    try:
        proc = subprocess.Popen(cmd, cwd=str(WORKDIR), env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", bufsize=1)
    except Exception as e:  # noqa: BLE001
        return False, {"error": "启动失败", "detail": str(e)[:500], "model": model}

    def _drain_err():
        try:
            for ln in proc.stderr:
                err_box.append(ln)
        except Exception:  # noqa: BLE001
            pass

    def _drain_out():
        try:
            with open(live_path, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "_meta", "agent": agent, "task_id": task_id,
                                     "model": model,
                                     "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                    ensure_ascii=False) + "\n")
                fh.flush()
                for ln in proc.stdout:
                    out_lines.append(ln)
                    fh.write(ln)
                    fh.flush()
        except Exception:  # noqa: BLE001
            pass

    th_out = threading.Thread(target=_drain_out, daemon=True)
    th_err = threading.Thread(target=_drain_err, daemon=True)
    th_out.start()
    th_err.start()

    # FIX-002 心跳：任务运行期间每 30 秒向中枢写一次“仍在跑”，用于区分“在跑”与“卡死”
    hb_stop = threading.Event()
    hb_key = "task_heartbeat_%s" % (agent or "unknown")

    def _heartbeat():
        while not hb_stop.wait(30):
            try:
                api_put("/kv", {"key": hb_key, "value": {
                    "task_id": task_id, "state": "running",
                    "elapsed_sec": round(time.time() - t0, 1),
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}})
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_heartbeat, daemon=True).start()
    th_out.join(timeout=TASK_TIMEOUT)
    if th_out.is_alive():
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            th_out.join(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        try:
            with open(live_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "_timeout", "timeout_sec": TASK_TIMEOUT},
                                    ensure_ascii=False) + "\n")
                fh.flush()
        except Exception:  # noqa: BLE001
            pass
        return False, {"error": "timeout", "timeout_sec": TASK_TIMEOUT, "model": model}

    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass
    rc = proc.returncode if proc.returncode is not None else 0
    hb_stop.set()
    try:
        api_put("/kv", {"key": hb_key, "value": {
            "task_id": task_id, "state": "finished",
            "elapsed_sec": round(time.time() - t0, 1),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}})
    except Exception:  # noqa: BLE001
        pass
    elapsed = round(time.time() - t0, 1)
    raw = "".join(out_lines)
    proc_stderr = "".join(err_box)
    trace_rel = str(live_path.relative_to(BASE_DIR)).replace("\\", "/")
    if proc_stderr:
        try:
            with open(live_path, "a", encoding="utf-8") as fh:
                fh.write("\n" + json.dumps({"type": "_stderr", "text": proc_stderr[-4000:]},
                                           ensure_ascii=False) + "\n")
                fh.flush()
        except Exception:  # noqa: BLE001
            pass
    parsed = parse_result(raw)
    bad = bool(parsed.get("is_error")) or rc != 0
    payload = {
        "model": model,
        "elapsed_sec": elapsed,
        "exit_code": rc,
        "is_error": bool(parsed.get("is_error")),
        "num_turns": parsed.get("num_turns"),
        "result": parsed.get("result") if "result" in parsed else None,
        # 完整执行轨迹（含工具调用/思考）的相对路径，供看板点开查看
        "trace": trace_rel,
        # 没有终态 result（例如撞上 max-turns）时也必须留下原始尾巴，否则结果就丢了
        "detail": None,
    }
    if bad or payload["result"] is None:
        payload["detail"] = parsed.get("raw_tail") or proc_stderr[-2000:] or None
    return (not bad), payload


# ---------------- 单实例保护 ----------------
def _pid_alive(pid: int) -> bool:
    """Windows 下安全探测进程是否存活（os.kill(pid,0) 在 Windows 会杀进程，不可用）。"""
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:  # noqa: BLE001
        return False


def acquire_singleton(agent: str) -> bool:
    """同一 Agent 只允许一个守护进程，避免 supervisor 重复拉起导致任务双跑。"""
    lock = BASE_DIR / (".daemon_lock_%s.pid" % agent)
    if lock.exists():
        try:
            old = int((lock.read_text(encoding="utf-8").strip() or "0"))
        except Exception:  # noqa: BLE001
            old = 0
        if old and old != os.getpid() and _pid_alive(old):
            log("已有实例在跑（pid=%s），本进程退出，避免双跑" % old)
            return False
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return True


def _load_add_dirs(agent: str) -> list:
    """从 UTF-8 配置文件读额外授权目录（绕开 VBS/环境变量的中文编码问题）。

    文件：.daemon_add_dirs_<agent>.txt，每行一个目录，# 开头为注释。
    """
    f = BASE_DIR / (".daemon_add_dirs_%s.txt" % agent)
    if not f.is_file():
        return []
    dirs = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            dirs.append(line)
    return dirs


# ---------------- 状态持久化 ----------------
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(**kw) -> None:
    st = load_state()
    st.update(kw)
    try:
        STATE_FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        log("状态写入失败: %s" % e)


# ---------------- 任务处理 ----------------
def handle_task(task: dict) -> None:
    tid = task.get("task_id")
    status = task.get("status")
    if status not in ("pending", "claimed", "running"):
        log("跳过 %s（状态=%s）" % (tid, status))
        return

    if status == "pending":
        r = api_post("/tasks/%s/claim" % tid)
        if r.status_code != 200:
            log("认领失败 %s: %s" % (tid, r.text[:200]))
            return

    api_post("/tasks/%s/status" % tid, {"status": "running", "note": "daemon 已接受"})
    log("执行中 %s | %s" % (tid, task.get("title")))
    ok, payload = run_agent(task.get("agent_name") or "unknown", tid, build_prompt(task))
    text = payload.get("result")
    text = text if isinstance(text, str) else ""
    # ★ 提问识别：机器人若给出"待确认/请你确认"式回复，说明它没完成而是在等答复 →
    #   置 blocked 并把问题 surface 到中枢 KV（避免任务"看着 done、其实卡在提问"）
    looks_q = bool(text) and len(text) <= 1500 and ("？" in text or "?" in text) and any(
        k in text for k in ("请确认", "需要你", "请提供", "待确认", "确认一下", "需用户", "请你", "是否")
    )
    # FIX-001 ★先落结果、再打提问标记★
    # 根因：中枢 /result 只接受 pending/claimed/running 状态；若先把任务置 blocked，
    #       回执会撞 409、结果被丢弃（RULE-M1-185125 的 16 分钟成果即如此丢失）。
    # 顺序必须是：① 提交 /result（权威记录）→ ② 再置 blocked + 写 KV（提问信号）。
    r = api_post("/tasks/%s/result" % tid, {"status": "done" if ok else "failed", "result": payload})
    if r.status_code not in (200, 201):
        log("回执 %s → 异常 (%s)，结果可能未入库，请检查" % (tid, r.status_code))
    if ok and looks_q:
        api_post("/tasks/%s/status" % tid, {"status": "blocked", "note": text[:800]})
        try:
            api_put("/kv", {"key": "wb_question_%s" % (task.get("agent_name") or "unknown"),
                            "value": {"task_id": tid, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                      "question": text[:1500]}})
        except Exception:  # noqa: BLE001
            pass
        log("回执 %s → blocked(疑似提问，待用户确认) %.1fs" % (tid, payload.get("elapsed_sec") or 0))
        return
    log("回执 %s → %s (%s) %.1fs" % (tid, "done" if ok else "failed", r.status_code,
                                     payload.get("elapsed_sec") or 0))


def fetch_task(task_id: str) -> dict | None:
    try:
        for t in api_get("/tasks"):
            if t.get("task_id") == task_id:
                return t
    except Exception as e:
        log("查任务失败 %s: %s" % (task_id, e))
    return None


def drain_pending(agent: str) -> None:
    """补单：把本 Agent 尚未开工的 pending 任务处理一遍。

    只碰 pending —— claimed/running 一律不动，避免抢走别人手上正在做的活。
    """
    try:
        tasks = api_get("/tasks", {"agent": agent})
    except Exception as e:
        log("拉取待办失败: %s" % e)
        return
    for t in tasks:
        if t.get("status") == "pending":
            handle_task(t)


# ---------------- 主循环 ----------------
def main() -> None:
    ap = argparse.ArgumentParser(description="QuantHub 守护进程")
    ap.add_argument("--agent", default="strategy_1m", help="本守护进程服务的 Agent 名")
    ap.add_argument("--workspace", default="",
                    help="唤醒 WorkBuddy Agent 时的工作目录（默认用 daemon_workdir）；"
                         "接某条业务线时应指向该线的工作区")
    ap.add_argument("--drain", action="store_true",
                    help="启动时补单：把该 Agent 已有的 pending 任务也做掉（默认不开，避免误接手历史任务）")
    ap.add_argument("--once", action="store_true", help="只处理一遍待办后退出（自检用）")
    args = ap.parse_args()
    agent = args.agent

    # 断点文件按 Agent 分开，避免不同 Agent 互相串号
    global STATE_FILE, WORKDIR, ADD_DIRS
    STATE_FILE = BASE_DIR / (".daemon_state_%s.json" % agent)
    if args.workspace:
        WORKDIR = Path(args.workspace)
    ADD_DIRS = ADD_DIRS + _load_add_dirs(agent)

    log("启动 | agent=%s model=%s api=%s" % (agent, MODEL, API_BASE))
    log("执行工作区 | %s" % WORKDIR)
    log("权限=%s | 额外目录=%s" % (PERMISSION_MODE or "(默认)", ADD_DIRS or "(无)"))
    node, cli = find_node(), find_cli()
    log("WorkBuddy 依赖 | node=%s | cli=%s" % (node, cli))
    if not node or not cli:
        log("致命：找不到 WorkBuddy node/CLI，退出")
        return

    if not acquire_singleton(agent):
        return

    if args.drain:
        log("--drain 开启：补做该 Agent 的 pending 任务")
        drain_pending(agent)
    if args.once:
        log("--once 完成，退出")
        return

    last = int(load_state().get("last_id", 0) or 0)
    log("从事件 last_id=%s 续接" % last)

    while True:
        try:
            r = api_get("/events/wait", {"since": last, "agent": agent, "timeout": POLL_TIMEOUT})
            new_last = int(r.get("last_id") or last)
            if new_last != last:
                last = new_last
                save_state(last_id=last)
            for ev in r.get("events") or []:
                if ev.get("action") != "create_task":
                    continue
                tid = ev.get("ref_task_id")
                task = fetch_task(tid) if tid else None
                if task:
                    handle_task(task)
        except httpx.RequestError as e:
            log("中枢不可达，%s 后重试: %s" % (5, e))
            time.sleep(5)
        except Exception as e:  # noqa: BLE001
            log("轮询异常，5 秒后重试: %s: %s" % (type(e).__name__, e))
            time.sleep(5)


if __name__ == "__main__":
    main()
