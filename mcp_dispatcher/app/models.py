# -*- coding: utf-8 -*-
"""请求体模型（Pydantic）。"""
from typing import Any, Dict, Optional

from pydantic import BaseModel


class AgentUpdate(BaseModel):
    status: Optional[str] = None
    role: Optional[str] = None
    current_task: Optional[str] = None


class TaskCreate(BaseModel):
    task_id: str
    agent_name: str
    title: Optional[str] = None
    kind: str = "work"
    payload: Dict[str, Any] = {}
    priority: str = "medium"
    depends_on: Optional[str] = None


class TaskResult(BaseModel):
    result: Dict[str, Any] = {}
    status: str = "done"


class TaskStatus(BaseModel):
    status: str
    note: Optional[str] = None


class DecisionCreate(BaseModel):
    item: str
    decision: str
    decided_by: str = "总控"


class GateUpdate(BaseModel):
    decision: str
    reason: Optional[str] = None


class KvSet(BaseModel):
    key: str
    value: Any
