# Live Skill Execution Smoke Suite Implementation Plan (Depth)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Actually *call* each high-value skill's primary tool with valid args on a real fixture and assert it returns a usable, non-error, non-punt result — catching execution bugs the contract suite can't (crashes, wrong result shapes, the `repo-fault-localize` "file too large" punt on a tiny file).

**Architecture:** Builds on the harness from `2026-06-27-live-skill-contract-suite.md` (`tests/live/skill_harness.py`). Two groups: a **model-free deterministic core** (pure-compute code-intelligence skills — ast-search, ast-repo-map, repo-graph) that runs under `LIVE_TESTS=1` on any host with no GPU; and an **inference-guarded group** (skills that call the LLM — repo-fault-localize, code-review, critique, test-gen) that `require_service`-skips when `GEMMA_BASE` is unreachable. code-sandbox execution is already covered by the existing `tests/live/test_code_sandbox_run_tests_live.py` / `test_local_executor_subprocess_live.py`, so it's not duplicated here.

**Tech Stack:** Python 3.11, pytest + pytest-asyncio, the `tests/live/` LIVE_TESTS gate + `skill_harness.py`.

## Global Constraints

- `LIVE_TESTS=1`-gated; default-skipped. The deterministic core needs **no GPU/Redis/worker**; the inference group additionally needs a reachable `GEMMA_BASE` (else skip).
- Each spec registers the skill in-process, calls one tool with valid args on a `tmp_path` fixture, asserts, then tears the skill down (no subprocess leaks).
- A skill that can't register (deps absent) or — for the inference group — when `GEMMA_BASE` is down must **skip**, not fail.
- "Pass" = the tool ran and returned a usable result: `not isError` AND non-empty content. Known-answer / non-punt checks are layered on where we can assert them confidently. Never weaken an assertion to hide a real skill bug — a genuine failure (crash, empty result, "too large" punt) is the suite working; file it.
- This plan DEPENDS ON `2026-06-27-live-skill-contract-suite.md` having landed (it imports `tests/live/skill_harness.py`). Implement that plan first.
- stdout-sacred / no tiktoken / Chroma client-server — unchanged.

---

### Task 1: Extend the harness — call a tool, read the result, check inference availability

**Files:**
- Modify: `tests/live/skill_harness.py`
- Test: `tests/live/test_skill_harness.py`

**Interfaces:**
- Produces:
  - `result_text(result) -> str` — join the `.text` of an MCP `CallToolResult`'s content (pure).
  - `result_is_error(result) -> bool` — `bool(getattr(result, "isError", False))` (pure).
  - `async call_skill_tool(manifest, tool: str, arguments: dict, timeout: float = 60.0)` — register → `reg.call_tool(f"{manifest.name}.{tool}", arguments)` → teardown; returns the `CallToolResult`.
  - `inference_available() -> bool` — GET `${GEMMA_BASE}/health` (default `http://localhost:8000/v1` → strip `/v1`), 2s timeout, True on 2xx.

- [ ] **Step 1: Write the failing tests for the pure helpers**

Add to `tests/live/test_skill_harness.py`:

```python
from tests.live.skill_harness import result_text, result_is_error


class _C:
    def __init__(self, text): self.text = text


class _R:
    def __init__(self, content, is_error=False):
        self.content = content
        self.isError = is_error


def test_result_text_joins_content():
    r = _R([_C("hello"), _C("world")])
    assert result_text(r) == "hello\nworld"


def test_result_is_error_reads_flag():
    assert result_is_error(_R([], is_error=True)) is True
    assert result_is_error(_R([_C("ok")])) is False
```

- [ ] **Step 2: Run to verify they fail**

Run: `LIVE_TESTS=1 python -m pytest tests/live/test_skill_harness.py -k "result_text or result_is_error" -v`
Expected: FAIL — symbols missing.

- [ ] **Step 3: Implement**

Add to `tests/live/skill_harness.py`:

```python
import os
import urllib.request


def result_text(result) -> str:
    content = getattr(result, "content", None) or []
    return "\n".join(c.text for c in content if hasattr(c, "text"))


def result_is_error(result) -> bool:
    return bool(getattr(result, "isError", False))


async def call_skill_tool(manifest, tool: str, arguments: dict, timeout: float = 60.0):
    reg, sp = await register_skill(manifest, timeout=timeout)
    try:
        return await reg.call_tool(f"{manifest.name}.{tool}", arguments)
    finally:
        await teardown_skill(reg, sp)


def inference_available() -> bool:
    base = os.getenv("GEMMA_BASE", "http://localhost:8000/v1").rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=2) as resp:
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001
        return False
```

- [ ] **Step 4: Run to verify they pass**

Run: `LIVE_TESTS=1 python -m pytest tests/live/test_skill_harness.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/live/skill_harness.py tests/live/test_skill_harness.py
git commit -m "test(live): harness helpers — call_skill_tool, result_text, inference_available"
```

---

### Task 2: Model-free execution smoke (ast-search, ast-repo-map, repo-graph)

Pure-compute code-intelligence skills — run anywhere under LIVE_TESTS with no GPU. Each gets a tiny `tmp_path` fixture, a real tool call with valid args, and a not-error + non-empty-result assertion (plus a known-answer where confident).

**Files:**
- Create: `tests/live/test_skill_exec_modelfree_live.py`

**Interfaces:**
- Consumes: `manifest_by_name`, `call_skill_tool`, `result_text`, `result_is_error`, `register_skill`, `SkillRegisterError` from `skill_harness`.

- [ ] **Step 1: Add a `manifest_by_name` lookup to the harness**

In `tests/live/skill_harness.py` add:

```python
def manifest_by_name(name: str):
    for m in runnable_manifests():
        if m.name == name:
            return m
    return None
```

- [ ] **Step 2: Write the model-free execution tests**

Create `tests/live/test_skill_exec_modelfree_live.py`:

```python
import pytest

from tests.live.conftest import require_service
from tests.live.skill_harness import (
    manifest_by_name, call_skill_tool, result_text, result_is_error,
    SkillRegisterError,
)

pytestmark = pytest.mark.live

_FIXTURE = (
    'def last_index(seq, target):\n'
    '    """Return the index of the LAST occurrence of target, or -1."""\n'
    '    for i in range(len(seq)):\n'
    '        if seq[i] == target:\n'
    '            return i\n'
    '    return -1\n'
)


async def _run(skill_name, tool, args, timeout=60.0):
    m = manifest_by_name(skill_name)
    if m is None:
        require_service(lambda: False, f"{skill_name} not runnable")
    try:
        return await call_skill_tool(m, tool, args, timeout=timeout)
    except SkillRegisterError as exc:
        require_service(lambda: False, f"{skill_name} register ({exc})")


@pytest.mark.asyncio
async def test_ast_search_find_code(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text(_FIXTURE)
    r = await _run("ast-search", "find_code",
                   {"pattern": "def last_index($$$):", "language": "python", "path": str(f)})
    assert not result_is_error(r)
    assert result_text(r).strip(), "ast-search returned empty"
    # known-answer: the matched output references the function
    assert "last_index" in result_text(r)


@pytest.mark.asyncio
async def test_ast_repo_map_get_symbols(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text(_FIXTURE)
    r = await _run("ast-repo-map", "get_symbols", {"file": str(f)})
    assert not result_is_error(r)
    assert "last_index" in result_text(r)


@pytest.mark.asyncio
async def test_repo_graph_build(tmp_path):
    (tmp_path / "mod.py").write_text(_FIXTURE)
    r = await _run("repo-graph", "build", {"repo_path": str(tmp_path)})
    assert not result_is_error(r)
    assert result_text(r).strip(), "repo-graph build returned empty"
```

- [ ] **Step 3: Run**

Run: `LIVE_TESTS=1 python -m pytest tests/live/test_skill_exec_modelfree_live.py -v`
Expected: PASS or SKIP (skip only if a skill can't register). A genuine FAIL (error result, empty output, missing symbol) is a real skill bug — record it.

> Implementer note: ast-grep pattern syntax — `def last_index($$$):` matches the fixture's def. If a skill's actual result shape makes the `"last_index" in text` check wrong (e.g. it returns line numbers only), KEEP `not result_is_error` + non-empty, drop the symbol-substring check, and note the real shape in a comment — do NOT invent a passing assertion. If `get_symbols`/`build` requires a differently-named arg than the SKILL.md/server shows, fix the arg to match the server's schema (read `services/skills/<name>/server.py`).

- [ ] **Step 4: Commit**

```bash
git add tests/live/skill_harness.py tests/live/test_skill_exec_modelfree_live.py
git commit -m "test(live): model-free execution smoke (ast-search, ast-repo-map, repo-graph)"
```

---

### Task 3: Inference-guarded execution smoke (repo-fault-localize, code-review, critique, test-gen)

These call the LLM, so they `require_service(inference_available)` and skip without a reachable `GEMMA_BASE`. The repo-fault-localize spec is the **"file too large" bug-catcher** (it must localize a tiny fixture, not punt).

**Files:**
- Create: `tests/live/test_skill_exec_inference_live.py`

- [ ] **Step 1: Write the inference-guarded tests**

Create `tests/live/test_skill_exec_inference_live.py`:

```python
import pytest

from tests.live.conftest import require_service
from tests.live.skill_harness import (
    manifest_by_name, call_skill_tool, result_text, result_is_error,
    inference_available, SkillRegisterError,
)

pytestmark = pytest.mark.live

_BUG = (
    'def last_index(seq, target):\n'
    '    """Return the index of the LAST occurrence of target, or -1."""\n'
    '    for i in range(len(seq)):\n'
    '        if seq[i] == target:\n'
    '            return i  # bug: returns FIRST match, not last\n'
    '    return -1\n'
)


async def _run(skill_name, tool, args, timeout=120.0):
    require_service(inference_available, "GEMMA_BASE inference server")
    m = manifest_by_name(skill_name)
    if m is None:
        require_service(lambda: False, f"{skill_name} not runnable")
    try:
        return await call_skill_tool(m, tool, args, timeout=timeout)
    except SkillRegisterError as exc:
        require_service(lambda: False, f"{skill_name} register ({exc})")


@pytest.mark.asyncio
async def test_repo_fault_localize_does_not_punt_on_tiny_file(tmp_path):
    (tmp_path / "off.py").write_text(_BUG)
    r = await _run("repo-fault-localize", "locate_files",
                   {"issue": "last_index returns the first match instead of the last",
                    "repo_path": str(tmp_path)})
    assert not result_is_error(r)
    text = result_text(r).lower()
    # the "file too large" punt on a 7-line file is the bug we are guarding against
    assert "too large" not in text and "send a snippet" not in text, \
        "repo-fault-localize punted 'file too large' on a tiny file"
    assert "off.py" in result_text(r), "did not localize the fixture file"


@pytest.mark.asyncio
async def test_critique_runs(tmp_path):
    r = await _run("critique", "critique",
                   {"output": "2 + 2 = 5", "task": "Compute 2 + 2 and report the result."})
    assert not result_is_error(r)
    assert result_text(r).strip(), "critique returned empty"


@pytest.mark.asyncio
async def test_code_review_runs():
    diff = (
        "--- a/m.py\n+++ b/m.py\n@@\n-def avg(x):\n-    return sum(x)/len(x)\n"
        "+def avg(x):\n+    return sum(x)/len(x) + 1\n"
    )
    r = await _run("code-review", "code_review", {"diff": diff})
    assert not result_is_error(r)
    assert result_text(r).strip(), "code-review returned empty"


@pytest.mark.asyncio
async def test_test_gen_generate(tmp_path):
    src = tmp_path / "calc.py"
    src.write_text("def add(a, b):\n    return a + b\n")
    r = await _run("test-gen", "generate", {"source_file": str(src)})
    assert not result_is_error(r)
    assert result_text(r).strip(), "test-gen returned empty"
```

- [ ] **Step 2: Run (skips without GEMMA_BASE — that's expected off-host)**

Run: `LIVE_TESTS=1 python -m pytest tests/live/test_skill_exec_inference_live.py -v`
Expected (no inference server): all SKIPPED. On a host with `GEMMA_BASE` up: PASS, or a real FAIL to file (e.g. repo-fault-localize punting).

> Implementer note: verify each tool's exact arg names against `services/skills/<name>/server.py` and fix any mismatch (e.g. if `code_review` requires `path` not `diff`, or `critique`'s args differ). Keep the assertions as written; only correct the arg keys to match the server's schema.

- [ ] **Step 3: Commit**

```bash
git add tests/live/test_skill_exec_inference_live.py
git commit -m "test(live): inference-guarded execution smoke (fault-localize/review/critique/test-gen)"
```

---

### Task 4: Document + confirm default-skipped

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Confirm gating**

Run: `python -m pytest tests/live -q` (no LIVE_TESTS) — Expected: all SKIPPED, exit 0.
Run: `python -m pytest tests/ -q 2>&1 | tail -5` — Expected: full suite green, live tests skipped.

- [ ] **Step 2: Document**

In `CLAUDE.md`, extend the live-tests subsection:

```markdown
Execution smoke (calls real tools on fixtures):
    # model-free core (no GPU): ast-search, ast-repo-map, repo-graph
    LIVE_TESTS=1 python -m pytest tests/live/test_skill_exec_modelfree_live.py -v
    # inference-guarded (needs GEMMA_BASE up): repo-fault-localize, code-review, critique, test-gen
    LIVE_TESTS=1 python -m pytest tests/live/test_skill_exec_inference_live.py -v
The inference group SKIPS when GEMMA_BASE is unreachable. repo-fault-localize's
test guards the "file too large on a tiny file" punt.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(CLAUDE.md): document live skill execution smoke suite"
```

---

## Self-Review

- **Spec coverage:** harness extension (Task 1) → model-free core (Task 2) → inference-guarded incl. the fault-localize bug-catcher (Task 3) → docs (Task 4). ✓
- **Honest gating:** deterministic core needs no GPU; inference group skips without `GEMMA_BASE`; dep-missing skills skip. Only real execution bugs fail. ✓
- **No fake passes:** implementer notes forbid inventing assertions to mask a real failure; arg-key corrections against the real server schema are allowed, assertion-weakening is not. ✓
- **Dependency:** requires the contract-suite plan's `skill_harness.py`; sequence contract → execution. ✓
- **Type consistency:** uses `CallToolResult.content[].text` / `.isError` (the real shape `SkillRegistry.call_tool` returns) and real arg names pulled from each server (`find_code`/`get_symbols`/`build`/`locate_files`/`code_review`/`critique`/`generate`). ✓
