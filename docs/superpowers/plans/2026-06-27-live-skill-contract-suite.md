# Live Skill Contract Suite Implementation Plan (Breadth)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A model-free, LIVE_TESTS-gated suite that registers **every runnable skill** and checks its tool *contract* — it starts, advertises the tools its `SKILL.md` declares, exposes valid input schemas, and returns an enumerated error for an unknown tool. This catches dead skills, manifest↔SKILL.md drift, and broken schemas across the whole catalog deterministically, where the A/B only stumbled on the ~5 skills its 5 cases happened to use.

**Architecture:** The A/B eval is a *selection/completion* benchmark — model-driven, non-deterministic, GPU-bound, and only exercises whatever tools the model happens to call. Tool **correctness** needs a different instrument. `SkillRegistry.register(manifest)` spawns the skill's MCP subprocess in the current event loop (`_run_skill` → `list_tools` → `sp.tools`, `state=READY`), so a `pytest-asyncio` test can register a skill in-process and inspect/call its tools with **no model, no Redis, no worker, no GPU**. This plan builds the harness + the catalog-wide *contract* checks (breadth). The companion plan (`2026-06-27-live-skill-execution-smoke.md`) adds per-skill execution assertions (depth).

**Tech Stack:** Python 3.11, pytest + pytest-asyncio, the MCP stdio client (already a dep), the existing `tests/live/` LIVE_TESTS gate.

## Global Constraints

- `LIVE_TESTS=1`-gated; default-skipped (autouse `_live_gate` in `tests/live/conftest.py`). No GPU / inference server / Redis / worker needed — only the skills' own Python/Node deps.
- A skill that can't register (missing deps, e.g. figma token, network model) must **skip**, not fail — use `require_service`-style skips so the suite is green on a partial host and only RED on a real contract break.
- Parametrize over `services.skill_worker.manifest_loader.discover_manifests()` — the exact set the worker registers (skips instruction-only and unbuilt-TS skills).
- Pure helpers (SKILL.md parsing) are unit-testable without LIVE_TESTS. The register/teardown helpers are live-only.
- Always tear down each registered skill (`sp._run_task.cancel()`) so subprocesses don't leak.
- stdout-sacred / no tiktoken / Chroma client-server — unchanged.

---

### Task 1: Skill test harness (manifests, SKILL.md parsing, register/teardown)

**Files:**
- Create: `tests/live/skill_harness.py`
- Test: `tests/live/test_skill_harness.py`

**Interfaces:**
- Produces:
  - `runnable_manifests() -> list[SkillManifest]` — wraps `discover_manifests` against `services/skills`.
  - `declared_tools(skill_name: str) -> set[str]` — parses the `tools:` list from `services/skills/<name>/SKILL.md` frontmatter (pure).
  - `async register_skill(manifest, timeout: float = 30.0) -> tuple[SkillRegistry, SkillProcess]` — registers, polls `sp.state` to `READY`; raises `SkillRegisterError` on `DEAD`/timeout.
  - `async teardown_skill(reg, sp) -> None` — cancels the skill's run task.
  - `SkillRegisterError(Exception)`.

- [ ] **Step 1: Write the failing test for the pure parser**

Create `tests/live/test_skill_harness.py`:

```python
import pytest
from tests.live.skill_harness import declared_tools, runnable_manifests

pytestmark = pytest.mark.live


def test_declared_tools_parses_code_sandbox():
    tools = declared_tools("code-sandbox")
    assert {"run_python", "run_shell", "run_tests", "install_packages"} <= tools


def test_declared_tools_unknown_skill_is_empty():
    assert declared_tools("does-not-exist") == set()


def test_runnable_manifests_includes_code_sandbox():
    names = {m.name for m in runnable_manifests()}
    assert "code-sandbox" in names
    # instruction-only skills are excluded
    assert "academic-writing" not in names
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `LIVE_TESTS=1 python -m pytest tests/live/test_skill_harness.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the harness**

Create `tests/live/skill_harness.py`:

```python
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
    await reg.register(manifest)
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
```

- [ ] **Step 4: Run the parser tests to verify they pass**

Run: `LIVE_TESTS=1 python -m pytest tests/live/test_skill_harness.py -v`
Expected: PASS. (The two parser tests don't need a subprocess; `runnable_manifests` just reads the fs.)

- [ ] **Step 5: Commit**

```bash
git add tests/live/skill_harness.py tests/live/test_skill_harness.py
git commit -m "test(live): skill test harness (discover, parse SKILL.md, register/teardown)"
```

---

### Task 2: Catalog-wide tool contract test

For every runnable skill: it reaches READY, advertises ≥1 tool, every advertised tool has a valid JSON-Schema input, and every tool its SKILL.md *declares* is actually advertised (catches doc↔server drift). A skill that can't register (deps absent) skips.

**Files:**
- Create: `tests/live/test_skill_contract_live.py`

- [ ] **Step 1: Write the contract test**

Create `tests/live/test_skill_contract_live.py`:

```python
import pytest

from tests.live.conftest import require_service
from tests.live.skill_harness import (
    runnable_manifests,
    declared_tools,
    register_skill,
    teardown_skill,
    SkillRegisterError,
)

pytestmark = pytest.mark.live

MANIFESTS = runnable_manifests()


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest", MANIFESTS, ids=[m.name for m in MANIFESTS])
async def test_skill_contract(manifest):
    try:
        reg, sp = await register_skill(manifest)
    except SkillRegisterError as exc:
        require_service(lambda: False, f"{manifest.name} register ({exc})")
        return
    try:
        # 1. advertises at least one tool
        assert sp.tools, f"{manifest.name} advertises no tools"
        # 2. every advertised tool has a JSON-Schema object input
        for tname, schema in sp.tools.items():
            assert isinstance(schema, dict), f"{manifest.name}.{tname} schema not a dict"
            assert schema.get("type") == "object", f"{manifest.name}.{tname} schema not an object"
        # 3. every SKILL.md-declared tool is actually advertised (doc<->server drift)
        missing = declared_tools(manifest.name) - set(sp.tools)
        assert not missing, f"{manifest.name}: SKILL.md declares tools not served: {missing}"
    finally:
        await teardown_skill(reg, sp)
```

- [ ] **Step 2: Run the contract test**

Run: `LIVE_TESTS=1 python -m pytest tests/live/test_skill_contract_live.py -v`
Expected: each runnable skill PASS or SKIP (skills whose deps are absent). Any genuine contract break (no tools, bad schema, declared-but-missing tool) FAILS loudly — that is the suite doing its job; if a pre-existing skill fails, capture it as a real bug to file (do NOT weaken the assertion to make it pass).

> Implementer note: if a skill legitimately advertises *more* tools than its SKILL.md lists, that is allowed (the assertion is declared ⊆ advertised, not equality). If a skill fails the schema check because its server returns a non-object top-level schema, that is a real skill bug — record it; do not relax the check.

- [ ] **Step 3: Commit**

```bash
git add tests/live/test_skill_contract_live.py
git commit -m "test(live): catalog-wide skill tool-contract check"
```

---

### Task 3: Unknown-tool discoverability contract (per skill)

Asserts the fix we shipped (`skill_registry.py` enumerates valid tool names) holds for every runnable skill — so a future model's wrong guess always gets a self-correcting error.

**Files:**
- Create: `tests/live/test_skill_unknown_tool_live.py`

- [ ] **Step 1: Write the test**

Create `tests/live/test_skill_unknown_tool_live.py`:

```python
import pytest

from tests.live.conftest import require_service
from tests.live.skill_harness import (
    runnable_manifests, register_skill, teardown_skill, SkillRegisterError,
)
from services.skill_runner.skill_registry import SkillUnavailable

pytestmark = pytest.mark.live

MANIFESTS = runnable_manifests()


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest", MANIFESTS, ids=[m.name for m in MANIFESTS])
async def test_unknown_tool_enumerates_valid_names(manifest):
    try:
        reg, sp = await register_skill(manifest)
    except SkillRegisterError as exc:
        require_service(lambda: False, f"{manifest.name} register ({exc})")
        return
    try:
        with pytest.raises(SkillUnavailable) as exc:
            await reg.call_tool(f"{manifest.name}.__definitely_not_a_tool__", {})
        msg = str(exc.value)
        assert "__definitely_not_a_tool__" in msg
        # at least one real tool name appears in the enumerated list
        assert any(t in msg for t in sp.tools), f"{manifest.name}: error did not enumerate valid tools"
    finally:
        await teardown_skill(reg, sp)
```

- [ ] **Step 2: Run**

Run: `LIVE_TESTS=1 python -m pytest tests/live/test_skill_unknown_tool_live.py -v`
Expected: PASS/SKIP across the catalog.

- [ ] **Step 3: Commit**

```bash
git add tests/live/test_skill_unknown_tool_live.py
git commit -m "test(live): unknown-tool enumerated-error contract across the catalog"
```

---

### Task 4: Document + confirm default-skipped

**Files:**
- Modify: `CLAUDE.md` (the live-tests section)

- [ ] **Step 1: Confirm default-skipped**

Run: `python -m pytest tests/live -q` (no LIVE_TESTS) — Expected: all SKIPPED, exit 0.
Run: `python -m pytest tests/ -q 2>&1 | tail -5` — Expected: full suite green, live tests skipped, no collection errors.

- [ ] **Step 2: Document**

In `CLAUDE.md`, under the live-tests subsection, add:

```markdown
The live skill suite registers every runnable skill (no model/Redis/GPU) and
checks its tool contract — advertised tools match SKILL.md, schemas are valid,
unknown tools return an enumerated error:

    LIVE_TESTS=1 python -m pytest tests/live/test_skill_contract_live.py \
      tests/live/test_skill_unknown_tool_live.py -v

A skill whose deps are absent SKIPS (not fails). Run this when adding/changing
any skill — it's the deterministic tool-correctness check the model-driven A/B
can't be (the A/B only exercises the few skills its cases happen to call).
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE.md): document live skill contract suite"
```

---

## Self-Review

- **Spec coverage:** harness (Task 1) → catalog contract (Task 2) → discoverability contract (Task 3) → docs/default-skipped (Task 4). ✓
- **Right instrument:** model-free, deterministic, no GPU/Redis — runs the whole catalog, unlike the A/B. Failures point at the skill, not at routing. ✓
- **Safe on partial hosts:** dep-missing skills skip via `require_service`; only real contract breaks fail. ✓
- **No silent weakening:** the implementer note forbids relaxing assertions to paper over a pre-existing skill bug — those get filed, not hidden. ✓
- **Type consistency:** uses real `SkillRegistry`/`SkillManifest`/`SkillProcess` + `discover_manifests`; teardown cancels `sp._run_task` (the real field). ✓
