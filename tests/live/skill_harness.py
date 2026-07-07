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
    SkillManifest,
    SkillProcess,
    SkillRegistry,
)
from services.skill_worker.manifest_loader import discover_manifests

SKILLS_ROOT = Path(__file__).resolve().parents[2] / "services" / "skills"


class SkillRegisterError(Exception):
    """A skill failed to reach READY (missing deps, crash, or timeout)."""


def runnable_manifests() -> list[SkillManifest]:
    return discover_manifests(SKILLS_ROOT)


def manifest_by_name(name: str):
    for m in runnable_manifests():
        if m.name == name:
            return m
    return None


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


def _dist_stale(dist: Path, src_dir: Path) -> bool:
    """True if a compiled dist artifact is older than any TypeScript source.

    A stale build means the running server serves OLD tool names while SKILL.md/
    src track the new ones — which surfaces as a confusing "declares tools not
    served" contract failure. Detecting it lets the suite skip with an actionable
    message instead. Missing dist/src -> not stale (let registration handle it).
    """
    if not dist.exists() or not src_dir.exists():
        return False
    dist_mtime = dist.stat().st_mtime
    return any(ts.stat().st_mtime > dist_mtime for ts in src_dir.rglob("*.ts"))


def node_build_is_stale(manifest: SkillManifest) -> bool:
    """True if a node skill's dist/index.js is older than its src (needs rebuild)."""
    if getattr(manifest, "command", "") != "node":
        return False
    d = SKILLS_ROOT / manifest.name
    return _dist_stale(d / "dist" / "index.js", d / "src")


# Hard ceiling (seconds) on tearing down one skill's subprocess. The production
# stdio_client __aexit__ awaits the child process exit; a skill that ignores
# cancellation (e.g. a node server stuck in a syscall) would otherwise block the
# whole suite forever — which is the multi-hour `tests/live` hang this guards.
TEARDOWN_TIMEOUT = 15.0


async def _register_skill_inner(
    manifest: SkillManifest, timeout: float
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


async def register_skill(
    manifest: SkillManifest, timeout: float = 30.0
) -> tuple[SkillRegistry, SkillProcess]:
    if node_build_is_stale(manifest):
        raise SkillRegisterError(
            f"{manifest.name}: stale node build (src newer than dist/index.js) — "
            f"run `npm run build` in services/skills/{manifest.name}"
        )
    # Hard outer ceiling: the registry's own _spawn() has a 30s ready-wait, but a
    # subprocess that hangs mid-handshake (before _ready is awaited) or a wedged
    # asyncio primitive could still block past it. wait_for caps the whole
    # register+ready-poll so one bad skill becomes a SKIP, not a suite-wide hang.
    try:
        return await asyncio.wait_for(
            _register_skill_inner(manifest, timeout), timeout=timeout + 5.0
        )
    except TimeoutError as exc:
        raise SkillRegisterError(
            f"{manifest.name}: registration exceeded {timeout + 5.0}s hard ceiling"
        ) from exc


async def teardown_skill(reg: SkillRegistry, sp: SkillProcess) -> None:
    task = getattr(sp, "_run_task", None)
    if task is None:
        return
    task.cancel()
    try:
        # Bound the cancellation join: if the subprocess refuses to die, stop
        # waiting and orphan the task rather than hang the suite. On timeout the
        # task stays cancelled and the event loop reaps it (or the child exit)
        # eventually — better an orphaned task than a multi-hour suite hang.
        await asyncio.wait_for(task, timeout=TEARDOWN_TIMEOUT)
    except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


def result_text(result) -> str:
    """Join the .text of an MCP CallToolResult's content."""
    content = getattr(result, "content", None) or []
    return "\n".join(c.text for c in content if hasattr(c, "text"))


def result_is_error(result) -> bool:
    """Check if an MCP CallToolResult is an error."""
    return bool(getattr(result, "isError", False))


async def call_skill_tool(
    manifest: SkillManifest, tool: str, arguments: dict, timeout: float = 60.0
):
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
        # A RunPod proxy (split Mac->pod topology) 403s the default Python-urllib
        # User-Agent, so this probe would wrongly report "unreachable" and SKIP every
        # inference-guarded skill test even though the model is up (the skills use
        # httpx/litellm, which the proxy allows). Send a curl-style UA. The timeout is
        # generous enough for a remote proxy round-trip (localhost is effectively instant).
        req = urllib.request.Request(f"{base}/health", headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001
        return False
