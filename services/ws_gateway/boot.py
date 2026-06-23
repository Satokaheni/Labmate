from __future__ import annotations

import os
from typing import Awaitable, Callable

import redis.asyncio as aioredis

# (state, detail, message)
CheckResult = tuple[str, str, str]
CheckFn = Callable[..., Awaitable[CheckResult]]

_PLAN = [
    {"id": "brain", "label": "Brain", "detail": "llama.cpp", "required": True},
    {"id": "nervous_system", "label": "Nervous System", "detail": "MCP bridge", "required": True},
    {"id": "hands", "label": "Hands", "detail": "skills", "required": False},
    {"id": "memory", "label": "Memory", "detail": "Redis + Mongo", "required": True},
    {"id": "workspace", "label": "Workspace", "detail": "config", "required": False},
]


def boot_plan() -> list[dict]:
    """Return the initial subsystem plan (all pending)."""
    return [{**s, "state": "pending"} for s in _PLAN]


async def check_brain(*, http_get: Callable[[str], Awaitable], base_url: str | None = None) -> CheckResult:
    url = (base_url or os.getenv("GEMMA_BASE", "http://localhost:8000")).rstrip("/")
    try:
        resp = await http_get(f"{url}/healthz")
        if getattr(resp, "status", 500) == 200:
            return ("ready", "llama.cpp reachable", "")
        return ("degraded", "llama.cpp non-200", f"status {getattr(resp, 'status', '?')}")
    except Exception as exc:  # noqa: BLE001 — best-effort health check
        return ("failed", "llama.cpp unreachable", str(exc))


async def check_nervous_system(*, mcp_ready: bool = True, tools: int = 0) -> CheckResult:
    if mcp_ready:
        return ("ready", f"MCP bridge · {tools} tools", "")
    return ("failed", "MCP bridge down", "bridge not ready")


async def check_hands(*, skills_dir: str | None = None) -> CheckResult:
    path = skills_dir or os.getenv("SKILLS_DIR", "services/skills")
    try:
        count = sum(1 for e in os.scandir(path) if e.is_dir())
    except FileNotFoundError:
        return ("degraded", "no skills dir", f"{path} missing")
    return ("ready", f"{count} skills", "")


async def check_memory(*, redis: aioredis.Redis) -> CheckResult:
    try:
        await redis.ping()
        return ("ready", "Redis reachable", "")
    except Exception as exc:  # noqa: BLE001
        return ("failed", "Redis unreachable", str(exc))


async def check_workspace(*, workspace_path: str | None = None) -> CheckResult:
    path = workspace_path or os.getenv("WORKSPACE_PATH", "/workspace")
    if os.path.isdir(path):
        return ("ready", path, "")
    return ("degraded", "workspace missing", f"{path} not found")


async def run_boot_sequence(
    emit: Callable[[dict], Awaitable[None]],
    checks: dict[str, CheckFn],
) -> bool:
    """Emit boot.plan, then per-subsystem starting/ready updates, then boot.ready.

    Returns True if all required subsystems reached a non-failed state.
    """
    plan = boot_plan()
    await emit({"type": "boot.plan", "subsystems": plan})

    all_required_ok = True
    for sub in plan:
        sid = sub["id"]
        await emit({"type": "boot.update", "id": sid, "state": "starting"})
        check = checks.get(sid)
        if check is None:
            state, detail, message = ("ready", sub["detail"], "")
        else:
            state, detail, message = await check()
        await emit(
            {"type": "boot.update", "id": sid, "state": state, "detail": detail, "message": message}
        )
        if sub["required"] and state == "failed":
            all_required_ok = False
            await emit({"type": "boot.error", "id": sid, "message": message})

    if all_required_ok:
        await emit(
            {
                "type": "boot.ready",
                "sessionBootstrap": {"sessions": [], "activeSessionId": None, "agentStatus": _idle_status()},
            }
        )
    return all_required_ok


def _idle_status() -> dict:
    return {
        "brain": {"model": os.getenv("BRAIN_MODEL", "gemma-31b"), "endpoint": os.getenv("GEMMA_BASE", ":8000"), "state": "idle", "node": "chat_node", "thinkingBudget": 2000},
        "nervousSystem": {"name": "MCP bridge", "transport": "stdio", "state": "connected", "toolsRegistered": 0},
        "hands": {"skills": []},
    }
