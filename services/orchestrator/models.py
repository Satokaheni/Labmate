from __future__ import annotations
import uuid
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)

def _uid() -> str:
    return str(uuid.uuid4())


class User(BaseModel):
    user_id: str = Field(default_factory=_uid)
    display_name: str
    created_at: datetime = Field(default_factory=_now)
    last_active: datetime = Field(default_factory=_now)


class Workspace(BaseModel):
    workspace_id: str = Field(default_factory=_uid)
    name: str
    user_id: str
    description: Optional[str] = None
    paths: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    instructions: Optional[str] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class SessionMeta(BaseModel):
    session_id: str
    user_id: str
    workspace_id: str
    task_preview: str = ""
    created_at: datetime = Field(default_factory=_now)
    completed_at: Optional[datetime] = None
    ok: bool = True
