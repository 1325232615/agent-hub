# -*- coding: utf-8 -*-
"""路径与令牌配置：FastAPI(api.py) 与 MCP(server.py) 共用。"""
import os
import secrets

# mcp_dispatcher/ 目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# quant_hub/ 目录
QUANT_HUB = os.path.dirname(BASE_DIR)

DB_PATH = os.environ.get("HUB_DB", os.path.join(BASE_DIR, "hub.db"))
TOKEN_FILE = os.path.join(BASE_DIR, ".hub_token")
API_BASE = os.environ.get("HUB_API", "http://127.0.0.1:8765")


def get_token() -> str:
    """优先环境变量 HUB_TOKEN，其次 .hub_token 文件。"""
    t = os.environ.get("HUB_TOKEN")
    if t:
        return t.strip()
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def ensure_token() -> str:
    """若不存在则生成一个随机令牌并落盘。"""
    t = get_token()
    if t:
        return t
    t = secrets.token_urlsafe(24)
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(t)
    return t
