# AgentHub · 多 Agent 调度中枢

> 用一个 **SQLite 权威库 + FastAPI + MCP + Web 看板**，把多个 AI 编码 Agent（跨不同平台/工具）编排成一支能**派活、能回执、能实时围观“思考过程”**的队伍。

一句话理解：过去是你一个人对着一堆 AI 窗口手工复制粘贴；现在你只对“中枢”下命令，中枢负责把任务分给对应的机器人、收回结果、并把复杂结论翻译成人话。

---

## 它解决什么问题

同时用多个 AI Agent（比如数据组、审计组、策略组各一个）时，会遇到四个老大难：

| 痛点 | AgentHub 的做法 |
|---|---|
| 任务派给了谁、做到哪一步了，全靠人脑记 | 所有任务落 **SQLite 权威库**，状态可查（pending/claimed/running/done/failed/blocked） |
| 多个窗口抢同一个任务，白烧算力 | **原子认领（claim）+ 严格校验状态码**，非 200 一律放弃 |
| “A 任务被 B 做掉了”串号 | 执行体**按 agent 分锁**，每个窗口显式**接管一个** agent，只认本窗口的事件 |
| 机器人到底“想了什么”看不到 | 执行轨迹按行落盘（NDJSON），**Web 看板 2 秒轮询实时回放**工具调用/思考 |

---

## 架构总览

```
                        ┌──────────────────────────────┐
   总控（人 / AI）  ──▶ │  MCP 工具层  server.py        │  派单 / 领单 / 回执 / 查状态
                        └──────────────┬───────────────┘
                                       │ HTTP (X-Hub-Token)
                        ┌──────────────▼───────────────┐
                        │  FastAPI  app/api.py         │  127.0.0.1:8765
                        │  ──▶ SQLite  hub.db（权威源） │
                        └──────────────┬───────────────┘
                         事件流 /events │ /tasks /kv /agents
              ┌─────────────────────────┼─────────────────────────┐
              ▼                                                   ▼
   ┌───────────────────────┐                         ┌───────────────────────┐
   │ 执行体 A：命令行守护    │                         │ 执行体 B：编辑器扩展    │
   │ hub_daemon.py          │                         │ extension.js           │
   │ 长轮询 → 调 Agent CLI  │                         │ 长轮询 → 注入会话       │
   └───────────┬───────────┘                         └───────────┬───────────┘
               │ 轨迹 NDJSON 落盘                                │
               └───────────────┬─────────────────────────────────┘
                               ▼
                    ┌───────────────────────┐
                    │ Web 看板 dashboard.py  │  127.0.0.1:8770
                    │ 实时轨迹 / 任务历史 /   │
                    │ 按机器人切换模型 / 对话 │
                    └───────────────────────┘
```

**分工要点**：中枢只管“调度与记录”，不亲自干活；执行体只管“把任务喂给 AI 并回执”。两者通过 HTTP + 令牌解耦，因此执行体可以跑在**任何平台**（命令行 CLI、编辑器插件……）。

---

## 目录结构

```
agent-hub/
├─ mcp_dispatcher/            # 调度中枢 + 命令行侧执行体
│  ├─ server.py              #   MCP 工具层（代理，转发到 FastAPI）
│  ├─ dashboard_server.py    #   Web 看板（含 /live 实时轨迹、/chat 对话）
│  ├─ hub_daemon.py          #   ★ 命令行侧执行体（长轮询 → 唤醒 Agent CLI → 回执）
│  ├─ seed_from_files.py     #   一次性种子导入（JSON → SQLite）
│  ├─ run_api.bat            #   启动中枢 FastAPI
│  ├─ run_daemon.bat         #   启动执行体
│  ├─ daemon_supervisor_*.vbs#   Windows 守护（崩溃自愈）
│  └─ app/
│     ├─ api.py              #   FastAPI 路由（tasks/agents/kv/events/snapshot）
│     ├─ db.py               #   SQLite 建表与连接
│     ├─ models.py           #   Pydantic 请求模型
│     ├─ config.py           #   令牌与路径
│     └─ bridge.py           #   SQLite → 只读 JSON 镜像
└─ trae_ext/agent-hub-bridge/ # 编辑器侧执行体（VS Code / Trae 兼容）
   ├─ extension.js           #   长轮询 + 按 agent 分锁 + 会话注入
   └─ package.json
```

---

## 快速开始

### 1) 装依赖（建议用虚拟环境）

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # macOS / Linux
```

### 2) 启动中枢（FastAPI）

```bash
cd mcp_dispatcher
.venv/Scripts/python -m uvicorn app.api:app --host 127.0.0.1 --port 8765
# 首次启动会自动生成令牌文件 .hub_token
```

### 3) 启动看板

```bash
cd mcp_dispatcher
.venv/Scripts/python dashboard_server.py --port 8770
# 浏览器打开 http://127.0.0.1:8770/
```

### 4) 启动一个执行体

```bash
cd mcp_dispatcher
.venv/Scripts/python hub_daemon.py --agent <你的agent名>
```

> `hub_daemon.py` 默认驱动一个外部 Agent CLI（`--model custom-local:xxx`，走任意 OpenAI 兼容端点）。
> 若你的 Agent 不是 CLI，可参考 `extension.js`（把“唤醒方式”换成注入编辑器会话）。

---

## 核心概念

- **Agent（机器人）**：一个逻辑岗位，如 `data_env` / `audit` / `strategy_xxx`。名字只是字符串，按需定义。
- **Task（任务）**：一次派活，生命周期 `pending → claimed → running → done/failed/blocked`。
- **KV**：中枢里的键值表，用来存**锁**（按 agent）、**心跳**、**模型选择**等运行态。
- **audit_gate（审计关口）**：一个全局开关，`NOT_PASS` 时禁止向高风险 Agent 派单——用于“先审计、后执行”的硬约束。
- **Trace（轨迹）**：执行体把 Agent 的每一步（思考/工具调用/结果）按行写成 NDJSON，看板据此实时回放。

### 任务生命周期（防重复领取的关键）

```
创建(pending) ──claim──▶ claimed ──running──▶ running ──result──▶ done / failed
     ▲                        │
     └──────── 认领失败(409) ──┘   ← 状态码严格校验，非 200 立即放弃，绝不硬跑
```

---

## 安全与隐私（重要）

本项目在开源前已做脱敏，**请你在自己的 fork 里继续遵守**：

- **令牌文件 `.hub_token` 绝不入库**（已在 `.gitignore`）。它是你对中枢写操作的唯一凭据。
- **不要提交任何个人绝对路径、真实账号、交易所/经纪商凭据、云服务 AccessKey**。
- 执行体的“额外授权目录”用独立配置文件（`.daemon_add_dirs_*.txt`）维护，不要写死进代码。
- 看板默认只监听 `127.0.0.1`，**不要**直接暴露到公网；如需远程访问，请自行加反向代理 + 鉴权。

---

## 已知约束

- 单机单库：SQLite 适合“一台机器上一支队伍”的场景；跨机部署需换成网络数据库。
- 看板实时刷新靠轮询（2~3 秒），不是推送；够用但非毫秒级。
- Windows 守护脚本（`.vbs`）只在 Windows 生效；其它系统可用 systemd / launchd 替代。

---

## License

MIT —— 见 [LICENSE](./LICENSE)。
