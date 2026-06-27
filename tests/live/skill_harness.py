"""Helpers for the live skill suites: discover, register, and inspect skills.

Model-free and Redis-free: SkillRegistry.register spawns the skill's MCP
subprocess in the current event loop, so a pytest-asyncio test can register a
skill and call its tools directly. Used by the contract suite (breadth) and the
execution-smoke suite (depth).
"""
from __future__ import annotations

import asyncio
import os
import re
import urllib.request
from pathlib import Path

from services.skill_runner.skill_registry import (
    SkillRegistry,
    SkillManifest,
    SkillProcess,
)
from services.skill_worker.manifest_loader import discover_manifests

SKILLS_ROOT = Path(__file__).resolve().parents[2] / "services" / "skills"


class SkillRegisterError(Exception):
    """A skill failed to reach READY (missing deps, crash, or timeout)."""


def runnable_manifests() -> list[SkillManifest]:
    return discover_manifests(SKILLS_ROOT)


def declared_tools(skill_name: str) -> set[str]:
    """Parse the `tools:` list from a skill's SKILL.md frontmatter."""
    md = SKILLS_ROOT / skill_name / "SKILL.md"
    if not md.exists():
        return set()
    text = md.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return set()
    front = m.group(1)
    tools: set[str] = set()
    in_tools = False
    for line in front.splitlines():
        if re.match(r"^tools\s*:", line):
            in_tools = True
            continue
        if in_tools:
            item = re.match(r"^\s*-\s*([A-Za-z0-9_\-]+)\s*$", line)
            if item:
                tools.add(item.group(1))
            elif re.match(r"^\S", line):  # next top-level key ends the list
                break
    return tools


async def register_skill(
    manifest: SkillManifest, timeout: float = 30.0
) -> tuple[SkillRegistry, SkillProcess]:
    reg = SkillRegistry(call_timeout=timeout)
    try:
        await reg.register(manifest)
    except Exception as exc:  # noqa: BLE001
        raise SkillRegisterError(f"{manifest.name}: registration failed ({exc})") from exc
    sp = reg._skills[manifest.name]
    deadline = asyncio.get_event_loop().time() + timeout
    while sp.state not in ("READY", "DEAD"):
        if asyncio.get_event_loop().time() > deadline:
            raise SkillRegisterError(f"{manifest.name}: not READY within {timeout}s")
        await asyncio.sleep(0.1)
    if sp.state != "READY":
        raise SkillRegisterError(f"{manifest.name}: registration failed (state={sp.state})")
    return reg, sp


async def teardown_skill(reg: SkillRegistry, sp: SkillProcess) -> None:
    task = getattr(sp, "_run_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


def result_text(result) -> str:
    """Join the .text of an MCP CallToolResult's content."""
    content = getattr(result, "content", None) or []
    return "\n".join(c.text for c in content if hasattr(c, "text"))


def result_is_error(result) -> bool:
    """Check if an MCP CallToolResult is an error."""
    return bool(getattr(result, "isError", False))


async def call_skill_tool(manifest: SkillManifest, tool: str, arguments: dict, timeout: float = 60.0):
    """Register a skill, call a tool, and teardown."""
    reg, sp = await register_skill(manifest, timeout=timeout)
    try:
        return await reg.call_tool(f"{manifest.name}.{tool}", arguments)
    finally:
        await teardown_skill(reg, sp)


def inference_available() -> bool:
    """Check if the inference server is available."""
    base = os.getenv("GEMMA_BASE", "http://localhost:8000/v1").rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=2) as resp:
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001
        return False
