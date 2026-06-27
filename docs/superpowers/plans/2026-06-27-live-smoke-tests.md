# Live Real-Seam Smoke Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a small, opt-in (`LIVE_TESTS=1`) suite that exercises the REAL execution seams — exec_run, code-sandbox, run_tests, tool-name discoverability — so the "green in mocks, broken live" class of bug that killed c1/c3 regresses loudly instead of silently.

**Architecture:** The whole harness is unit/BDD-tested against mocked seams (`fake_model`, stubbed `skill_router`/`mcp`). That is exactly why a doubly-dead `run_tests` shipped with passing tests. These smoke tests do NOT mock the seam: they call the real MCP-bridge `exec_run`, the real code-sandbox executor, and the real `SkillRegistry`, asserting the *contracts* the orchestrator depends on (pytest is blocked on exec_run; exec_run timeout caps at 60000; code-sandbox actually runs pytest; unknown tool names return an enumerated error). They are skipped by default (no services / no GPU needed for CI) and run on the RunPod host before an A/B.

**Tech Stack:** Python 3.11, pytest, the existing `live` marker in `pytest.ini`, Node MCP bridge (built `dist/index.js`), code-sandbox `LocalSubprocessExecutor`.

## Global Constraints

- Default-skipped: every test in this plan is gated so a normal `pytest tests/` run (CI, laptop, no services) never executes them.
- Gate condition: `LIVE_TESTS=1` env var. Tests that additionally need a running service self-skip (with a clear reason) if that service is unreachable, rather than hard-failing.
- No GPU and no inference server required — these test the *tool* seams, not the model.
- Tag every test with `@pytest.mark.live` (marker already declared in `pytest.ini`).
- stdout-sacred and all other repo rules still apply.

---

### Task 1: Shared `live` gating fixture/helper

**Files:**
- Create: `tests/live/__init__.py` (empty)
- Create: `tests/live/conftest.py`
- Test: (the helper is exercised by Tasks 2-4; add one self-test)
- Modify: `tests/live/test_gate_selftest.py` (new)

**Interfaces:**
- Produces: a `live_enabled()` helper + a module-level `pytestmark = [pytest.mark.live, pytest.mark.skipif(not LIVE, reason=…)]` pattern the other live files import; a `require_service(check: Callable[[], bool], name: str)` helper that `pytest.skip()`s when a needed service is down.

- [ ] **Step 1: Write the gate self-test (fails until the helper exists)**

Create `tests/live/test_gate_selftest.py`:

```python
import os
import pytest
from tests.live.conftest import live_enabled

pytestmark = pytest.mark.live


def test_live_gate_matches_env():
    assert live_enabled() == (os.getenv("LIVE_TESTS") == "1")
```

- [ ] **Step 2: Run to verify it fails**

Run: `LIVE_TESTS=1 python -m pytest tests/live/test_gate_selftest.py -v`
Expected: FAIL — `tests/live/conftest.py` / `live_enabled` does not exist.

- [ ] **Step 3: Implement the gate**

Create `tests/live/__init__.py` (empty). Create `tests/live/conftest.py`:

```python
"""Gating for LIVE real-seam smoke tests.

These exercise actual execution seams (exec_run, code-sandbox, SkillRegistry)
and are SKIPPED unless LIVE_TESTS=1. Run on the deployment host before an A/B:
    LIVE_TESTS=1 python -m pytest tests/live -v
"""
from __future__ import annotations

import os
from typing import Callable

import pytest


def live_enabled() -> bool:
    return os.getenv("LIVE_TESTS") == "1"


def require_live() -> None:
    if not live_enabled():
        pytest.skip("LIVE_TESTS!=1 (set LIVE_TESTS=1 to run real-seam smoke tests)")


def require_service(check: Callable[[], bool], name: str) -> None:
    """Skip (not fail) when a needed live service is unreachable."""
    try:
        ok = check()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live service {name!r} unreachable: {exc}")
    if not ok:
        pytest.skip(f"live service {name!r} not ready")


@pytest.fixture(autouse=True)
def _live_gate():
    require_live()
```

- [ ] **Step 4: Run to verify it passes (and that it skips when off)**

Run: `LIVE_TESTS=1 python -m pytest tests/live/test_gate_selftest.py -v` → PASS.
Run: `python -m pytest tests/live/test_gate_selftest.py -v` → SKIPPED.

- [ ] **Step 5: Commit**

```bash
git add tests/live/__init__.py tests/live/conftest.py tests/live/test_gate_selftest.py
git commit -m "test(live): add LIVE_TESTS gating for real-seam smoke tests"
```

---

### Task 2: code-sandbox actually runs pytest (the c1/c3 unblocker)

**Files:**
- Create: `tests/live/test_code_sandbox_run_tests_live.py`

**Interfaces:**
- Consumes: `tests/live/conftest.py` gate; the real `LocalSubprocessExecutor` from the code-sandbox skill (imported by path, like the existing code-sandbox unit tests).

- [ ] **Step 1: Write the live test**

Create `tests/live/test_code_sandbox_run_tests_live.py`:

```python
import os
import sys
import pytest

pytestmark = pytest.mark.live

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..",
                                "services", "skills", "code-sandbox"))
from executor import LocalSubprocessExecutor  # noqa: E402


def test_real_pytest_passes(tmp_path):
    t = tmp_path / "test_pass.py"
    t.write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    res = LocalSubprocessExecutor().run_tests(str(t))
    assert res.passed == 1
    assert res.failed == 0
    assert res.errors == 0
    assert not res.timed_out


def test_real_pytest_reports_failure(tmp_path):
    t = tmp_path / "test_fail.py"
    t.write_text("def test_bad():\n    assert 1 == 2\n")
    res = LocalSubprocessExecutor().run_tests(str(t))
    assert res.failed == 1
    assert res.passed == 0


def test_real_pytest_honors_k_expr(tmp_path):
    t = tmp_path / "test_two.py"
    t.write_text("def test_alpha():\n    assert True\n\ndef test_beta():\n    assert True\n")
    res = LocalSubprocessExecutor().run_tests(str(t), expr="alpha")
    assert res.passed == 1
```

- [ ] **Step 2: Run**

Run: `LIVE_TESTS=1 python -m pytest tests/live/test_code_sandbox_run_tests_live.py -v`
Expected: PASS (requires the toolchain-fix plan's Task 2 `expr` support; the first two pass independently).

- [ ] **Step 3: Commit**

```bash
git add tests/live/test_code_sandbox_run_tests_live.py
git commit -m "test(live): code-sandbox LocalSubprocessExecutor really runs pytest"
```

---

### Task 3: exec_run seam contracts (pytest blocked, timeout cap, plain command works)

These encode the two constraints that silently broke `run_tests` so any future change to `exec.ts` fails loudly here. Calls the real built MCP bridge over stdio.

**Files:**
- Create: `tests/live/test_exec_run_contract_live.py`

**Interfaces:**
- Consumes: the built bridge at `services/mcp-bridge/dist/index.js` and an MCP stdio client. Reuse the orchestrator's existing MCP client helper if one exists (search `services/orchestrator` for the bridge-spawn/connect code and import it); otherwise use the `mcp` python SDK `stdio_client` directly. `require_service` skips if `dist/index.js` is missing.

- [ ] **Step 1: Write the live contract test**

Create `tests/live/test_exec_run_contract_live.py`:

```python
import json
import os
import pytest

from tests.live.conftest import require_service

pytestmark = pytest.mark.live

BRIDGE = os.path.join(
    os.path.dirname(__file__), "..", "..", "services", "mcp-bridge", "dist", "index.js"
)


async def _call_exec_run(command: str, timeout: int):
    """Spawn the bridge over stdio and call exec_run once. Returns (text, isError)."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command="node", args=[BRIDGE], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool(
                "exec_run", {"command": command, "cwd": os.getcwd(), "timeout": timeout}
            )
            text = "\n".join(c.text for c in res.content if hasattr(c, "text"))
            return text, bool(res.isError)


@pytest.mark.asyncio
async def test_plain_command_runs():
    require_service(lambda: os.path.exists(BRIDGE), "mcp-bridge dist")
    text, is_error = await _call_exec_run("echo hello-live", 10000)
    assert not is_error
    assert "hello-live" in text


@pytest.mark.asyncio
async def test_pytest_is_blocked_through_exec_run():
    require_service(lambda: os.path.exists(BRIDGE), "mcp-bridge dist")
    text, is_error = await _call_exec_run("pytest -q", 10000)
    assert is_error
    assert "not allowed" in text.lower() or "code-sandbox" in text.lower()


@pytest.mark.asyncio
async def test_timeout_above_cap_is_rejected():
    require_service(lambda: os.path.exists(BRIDGE), "mcp-bridge dist")
    # exec_run schema caps timeout at 60000ms; 120000 must be rejected, not run.
    text, is_error = await _call_exec_run("echo x", 120000)
    assert is_error
```

- [ ] **Step 2: Run (build the bridge first if needed)**

Run: `cd services/mcp-bridge && npm run build && cd ../.. && LIVE_TESTS=1 python -m pytest tests/live/test_exec_run_contract_live.py -v`
Expected: PASS. If `node`/`dist` absent the tests SKIP (not fail).

- [ ] **Step 3: Commit**

```bash
git add tests/live/test_exec_run_contract_live.py
git commit -m "test(live): exec_run contracts — pytest blocked, timeout cap enforced"
```

---

### Task 4: code-sandbox tool-name discoverability (real registry)

Asserts the discoverability fix from the toolchain-fix plan (Task 4) end-to-end against the real `SkillRegistry` + code-sandbox MCP server.

**Files:**
- Create: `tests/live/test_skill_tool_discovery_live.py`

**Interfaces:**
- Consumes: `SkillRegistry` (real), the code-sandbox manifest. Reuse the registry bootstrap the worker uses (`services/skill_worker/manifest_loader.py` + `SkillRegistry.register`); `require_service` skips if registration fails (e.g. python deps for the skill missing).

- [ ] **Step 1: Write the live discovery test**

Create `tests/live/test_skill_tool_discovery_live.py`:

```python
import pytest

from tests.live.conftest import require_service
from services.skill_runner.skill_registry import SkillRegistry, SkillUnavailable

pytestmark = pytest.mark.live


async def _registry_with_code_sandbox() -> SkillRegistry:
    # Mirror the worker's bootstrap; adapt to manifest_loader's real API.
    from services.skill_worker.manifest_loader import load_manifests
    reg = SkillRegistry()
    manifests = load_manifests("services/skills")
    cs = next((m for m in manifests if m.name == "code-sandbox"), None)
    if cs is None:
        raise RuntimeError("code-sandbox manifest not found")
    await reg.register(cs)
    return reg


@pytest.mark.asyncio
async def test_code_sandbox_advertises_expected_tools():
    try:
        reg = await _registry_with_code_sandbox()
    except Exception as exc:  # noqa: BLE001
        require_service(lambda: False, f"code-sandbox registration ({exc})")
    sp = reg._skills["code-sandbox"]
    for name in ("run_python", "run_shell", "run_tests", "install_packages"):
        assert name in sp.tools


@pytest.mark.asyncio
async def test_unknown_tool_lists_valid_names():
    try:
        reg = await _registry_with_code_sandbox()
    except Exception as exc:  # noqa: BLE001
        require_service(lambda: False, f"code-sandbox registration ({exc})")
    with pytest.raises(SkillUnavailable) as exc:
        await reg.call_tool("code-sandbox.run_pytest", {"test_path": "x"})
    msg = str(exc.value)
    assert "run_pytest" in msg
    assert "run_tests" in msg
```

> Implementer note: read `services/skill_worker/manifest_loader.py` and `SkillRegistry.register` first and match their real signatures (the `load_manifests` name above is a placeholder — use the actual loader). If registration needs a running event loop / inbox pump that only the worker provides, fall back to asserting `sp.tools` after `register` and keep the unknown-tool assertion (it only needs `sp.tools` populated + the Task 4 enumerated error).

- [ ] **Step 2: Run**

Run: `LIVE_TESTS=1 python -m pytest tests/live/test_skill_tool_discovery_live.py -v`
Expected: PASS or SKIP (if the skill cannot register in the test host).

- [ ] **Step 3: Commit**

```bash
git add tests/live/test_skill_tool_discovery_live.py
git commit -m "test(live): code-sandbox tool discoverability + enumerated unknown-tool error"
```

---

### Task 5: Document the live smoke suite

**Files:**
- Modify: `CLAUDE.md` (the "Live E2E Verification" section, add a subsection after §8)

- [ ] **Step 1: Add the doc snippet**

Add to `CLAUDE.md` under the Live E2E Verification section:

```markdown
### 10. Live real-seam smoke tests (run on the host before an A/B)

These exercise the ACTUAL execution seams (not mocks), catching the
"green in mocks, broken live" class that the unit suite cannot. Skipped
unless `LIVE_TESTS=1`. No GPU / inference server needed.

    cd services/mcp-bridge && npm run build && cd ../..   # exec_run contract test needs dist/
    LIVE_TESTS=1 python -m pytest tests/live -v

Covers: code-sandbox really runs pytest; exec_run blocks pytest + enforces the
60000ms timeout cap; code-sandbox advertises run_python/run_shell/run_tests/
install_packages and unknown tool names return an enumerated error. Run these
GREEN before trusting an `eval/seq_ab` A/B run.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE.md): document LIVE_TESTS real-seam smoke suite"
```

---

### Task 6: Confirm default-skipped

- [ ] **Step 1:** Run: `python -m pytest tests/live -v` (no LIVE_TESTS) — Expected: all SKIPPED, exit 0.
- [ ] **Step 2:** Run: `python -m pytest tests/ -q 2>&1 | tail -5` — Expected: full suite green, live tests skipped, no collection errors.

---

## Self-Review

- **Spec coverage:** gate (Task 1); run_tests-really-runs (Task 2 — the c1/c3 unblocker proof); exec_run contracts (Task 3); discoverability (Task 4); docs (Task 5); default-skipped proof (Task 6). ✓
- **No CI breakage:** every test is `@pytest.mark.live` + autouse `require_live()` skip; unreachable services skip rather than fail. ✓
- **Dependency note:** Task 2's `expr` assertion and Task 4's enumerated-error assertion depend on the toolchain-fix plan landing first; sequence toolchain-fix → infra-aware → live-smoke. The first two assertions in Task 2 (pass/fail) and Task 3 (all) are independent and would have caught the original bug on their own.
- **Placeholder honesty:** the two "adapt to the real loader/constructor" notes (Task 4 registry bootstrap, Task 1 none) are explicitly flagged; everything else is concrete.
