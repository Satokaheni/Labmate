# First-class run_tests Tool + Reliable write_file Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the single-intent ReAct loop a first-class, always-available `run_tests` tool that runs a REAL pytest command and returns raw stdout/stderr, plus make `write_file` verify its own write by reading the file back — so the model can no longer fabricate "all tests pass" or claim "code was updated" when it wasn't.

**Architecture:** Two pure helpers are added to `services/orchestrator/local_tools.py` so they are unit-testable without a live model: `build_run_tests_command()` (arg-parsing → argv + cwd + timeout) and `shape_run_tests_result()` (raw subprocess output → `{ok, exit_code, raw_output}`). The `run_tests` tool is wired into `_run_react_loop` as a new flat tool dispatch branch that runs the command through the EXISTING server-side bash seam (`self.mcp.call_tool("exec_run", ...)`) — the same path `run_bash` already uses — so it inherits the sandbox rule (no new client round-trip, no host disk access). The `run_tests` tool schema is added to `PromptAssembler`'s static tail so it is part of the frozen, always-present prefix. `write_file` reliability is added as a post-write read-back inside the existing `LOCAL_TOOL_NAMES` dispatch branch in `_run_react_loop`: after the client confirms a `write_file`, the orchestrator issues a follow-up `read_file` for the same path and compares content; on mismatch it returns an explicit error string the model sees.

**Tech Stack:** Python 3.11, asyncio, litellm (mocked at the HTTP seam via respx), pytest + pytest-asyncio + pytest-bdd, fakeredis. No new third-party deps.

## Global Constraints

- **stdout is sacred:** never `print()` / `console.log` / write to stdout in any orchestrator or MCP path — JSON-RPC 2.0 lives there. Use `logging` to stderr only. (CLAUDE.md rule 1)
- **No tiktoken** anywhere. (CLAUDE.md)
- **asyncio-correct:** never call `asyncio.run()` inside an async function/context. `_run_react_loop` is already inside the running loop. (CLAUDE.md)
- **Every litellm call sets** `api_key="not-needed"` and `extra_body={"thinking_budget_tokens": ...}`. The `run_tests` tool issues NO new model call, so this only constrains code that already exists — do not add model calls.
- **Additive + regression-safe:** existing tools (`read_file`, `write_file`, `list_dir`, `run_bash`, `call_skill_tool`, `code_semantic_search`, `finish`, `load_skill`) keep their current schemas, dispatch behavior, and event emissions. `run_tests` is a NEW branch; the `write_file` read-back is layered ON TOP of the existing `LOCAL_TOOL_NAMES` branch without changing the success path's return when content matches.
- **Service URLs from env, never hardcoded.** The new env knobs default sanely and are read via `os.getenv`.
- **File naming:** Python files `snake_case.py`, functions `snake_case`, classes `PascalCase`. (CLAUDE.md)
- **Testing:** tests live under `tests/` mirroring `services/`; `pytest` + `pytest-asyncio` only; assert structure not literal LLM text; `@mocked` BDD scenarios use the `fake_model` respx fixture / patched `litellm.acompletion` seam — no GPU.
- **Env knobs (new, all optional):**
  - `LABMATE_TEST_CMD` (default `"pytest"`) — the base test runner argv token(s); split on whitespace.
  - `LABMATE_TEST_TIMEOUT_MS` (default `120000`) — exec timeout passed to the bash seam.

---

## File Map

| Path | Change | Responsibility |
|------|--------|----------------|
| `services/orchestrator/local_tools.py` | **Modify** | Add two PURE helpers: `build_run_tests_command(args)` and `shape_run_tests_result(exit_code, raw_output)`. Add module constants `RUN_TESTS_DEFAULT_CMD`, `RUN_TESTS_TIMEOUT_MS_DEFAULT`. Add `verify_written_content(requested, readback)` pure helper for the write_file read-back compare. Existing functions unchanged. |
| `services/orchestrator/prompt_assembler.py` | **Modify** | Add a `run_tests` tool schema into `_static_tail_schemas()` (frozen prefix, always present). Existing schemas/order otherwise preserved (append `run_tests` immediately before `finish`). |
| `services/orchestrator/coding_orchestrator.py` | **Modify** | In `_run_react_loop`: (a) add a `run_tests` dispatch branch that runs the command via `self.mcp.call_tool("exec_run", ...)` and returns shaped JSON; (b) add a post-write read-back to the `LOCAL_TOOL_NAMES` branch for `write_file` that issues a follow-up `read_file` and returns an explicit error on mismatch. |
| `tests/services/orchestrator/test_run_tests_tool.py` | **Create** | Unit tests for `build_run_tests_command`, `shape_run_tests_result`, `verify_written_content`, and the `PromptAssembler` schema presence. |
| `tests/services/orchestrator/test_local_tools.py` | **Modify** | Add unit tests for the write_file read-back verify helper (`verify_written_content`) alongside the existing local-tool tests. |
| `tests/services/orchestrator/features/run_tests_tool.feature` | **Create** | `@mocked` Gherkin scenarios: run_tests returns real exit code + raw output; failing suite surfaces failure text; write_file read-back catches a non-applied write. |
| `tests/services/orchestrator/test_run_tests_tool_bdd.py` | **Create** | pytest-bdd step defs binding the feature, patching the model seam (`coding_orchestrator.litellm.acompletion`) and the bash/local seams. |

---

## Behavior (BDD) — Gherkin

Full content of `tests/services/orchestrator/features/run_tests_tool.feature`:

```gherkin
@mocked
Feature: First-class run_tests tool and reliable write_file
  As the single-intent ReAct loop
  I want a flat run_tests tool that runs a real test command and returns raw output
  And a write_file that verifies its own write by reading it back
  So the model can verify a fix instead of fabricating "all tests pass" or "code updated"

  Scenario: run_tests is always available in the loop tool list
    Given an AsyncOrchestrator with no skill router and no mcp
    When the prompt assembler builds the tool list
    Then the tool list contains a tool named "run_tests"
    And the run_tests tool has a "path" parameter

  Scenario: run_tests returns the real exit code and raw output on success
    Given an AsyncOrchestrator with no skill router and a stub bash seam
    And the bash seam returns exit code 0 with output "2 passed in 0.03s"
    And the model calls run_tests with path "tests/" on turn 1
    And the model calls finish with summary "tests pass" on turn 2
    When react_execute runs the goal "run the tests"
    Then the tool result json has ok True
    And the tool result json has exit_code 0
    And the tool result raw_output contains "2 passed"

  Scenario: a failing suite surfaces the real failure text, not a summary
    Given an AsyncOrchestrator with no skill router and a stub bash seam
    And the bash seam returns exit code 1 with output "E   assert 1 == 2\n1 failed in 0.02s"
    And the model calls run_tests with path "tests/test_math.py" on turn 1
    And the model calls finish with summary "saw failure" on turn 2
    When react_execute runs the goal "run the failing tests"
    Then the tool result json has ok False
    And the tool result json has exit_code 1
    And the tool result raw_output contains "assert 1 == 2"

  Scenario: write_file read-back catches a write that did not apply
    Given an AsyncOrchestrator with no skill router and a local tool client
    And the write_file client reports success but the file reads back as "OLD CONTENT"
    And the model calls write_file with path "src/app.py" and content "NEW CONTENT" on turn 1
    And the model calls finish with summary "wrote file" on turn 2
    When react_execute runs the goal "update the file"
    Then the write_file tool result contains "write verification failed"
    And the write_file tool result contains "did not match"

  Scenario: write_file read-back confirms a write that did apply
    Given an AsyncOrchestrator with no skill router and a local tool client
    And the write_file client reports success and the file reads back as "NEW CONTENT"
    And the model calls write_file with path "src/app.py" and content "NEW CONTENT" on turn 1
    And the model calls finish with summary "wrote file" on turn 2
    When react_execute runs the goal "update the file"
    Then the write_file tool result contains "verified"
    And the write_file tool result does not contain "verification failed"
```

---

## Task 1: Pure run_tests helpers in local_tools.py

**Files:**
- Modify: `services/orchestrator/local_tools.py`
- Test: `tests/services/orchestrator/test_run_tests_tool.py`

**Interfaces:**
- Produces:
  - `RUN_TESTS_DEFAULT_CMD: str` — default base runner token, value `"pytest"`.
  - `RUN_TESTS_TIMEOUT_MS_DEFAULT: int` — value `120000`.
  - `build_run_tests_command(args: dict[str, Any]) -> tuple[str, int]` — returns `(command_string, timeout_ms)`. `command_string` is a shell string suitable for the `exec_run` bash seam (e.g. `"pytest tests/ -q"`). Reads `LABMATE_TEST_CMD` / `LABMATE_TEST_TIMEOUT_MS` from env. Accepts optional `args["path"]`, `args["expr"]` (pytest `-k` expression), and `args["timeout_ms"]`.
  - `shape_run_tests_result(exit_code: int, raw_output: str) -> dict[str, Any]` — returns `{"ok": bool, "exit_code": int, "raw_output": str}`; `ok` is `exit_code == 0`. `raw_output` is the RAW text, only tail-truncated to 8000 chars (NOT summarized).

- [ ] **Step 1: Write the failing tests**

Create `tests/services/orchestrator/test_run_tests_tool.py`:

```python
# tests/services/orchestrator/test_run_tests_tool.py
from __future__ import annotations

import pytest

from services.orchestrator.local_tools import (
    RUN_TESTS_DEFAULT_CMD,
    RUN_TESTS_TIMEOUT_MS_DEFAULT,
    build_run_tests_command,
    shape_run_tests_result,
)


def test_build_run_tests_command_defaults_to_pytest():
    cmd, timeout_ms = build_run_tests_command({})
    assert cmd == "pytest"
    assert timeout_ms == RUN_TESTS_TIMEOUT_MS_DEFAULT


def test_build_run_tests_command_appends_path():
    cmd, _ = build_run_tests_command({"path": "tests/test_math.py"})
    assert cmd == "pytest tests/test_math.py"


def test_build_run_tests_command_appends_k_expr():
    cmd, _ = build_run_tests_command({"path": "tests/", "expr": "factorial"})
    assert cmd == "pytest tests/ -k factorial"


def test_build_run_tests_command_quotes_multiword_expr():
    cmd, _ = build_run_tests_command({"expr": "add or sub"})
    # multi-word -k expression must be quoted as a single shell argument
    assert cmd == "pytest -k 'add or sub'"


def test_build_run_tests_command_honors_env_cmd(monkeypatch):
    monkeypatch.setenv("LABMATE_TEST_CMD", "python -m pytest")
    cmd, _ = build_run_tests_command({"path": "tests/"})
    assert cmd == "python -m pytest tests/"


def test_build_run_tests_command_honors_env_timeout(monkeypatch):
    monkeypatch.setenv("LABMATE_TEST_TIMEOUT_MS", "5000")
    _, timeout_ms = build_run_tests_command({})
    assert timeout_ms == 5000


def test_build_run_tests_command_arg_timeout_overrides_env(monkeypatch):
    monkeypatch.setenv("LABMATE_TEST_TIMEOUT_MS", "5000")
    _, timeout_ms = build_run_tests_command({"timeout_ms": 9000})
    assert timeout_ms == 9000


def test_default_cmd_constant_is_pytest():
    assert RUN_TESTS_DEFAULT_CMD == "pytest"


def test_shape_run_tests_result_ok_on_zero_exit():
    out = shape_run_tests_result(0, "3 passed in 0.04s")
    assert out == {"ok": True, "exit_code": 0, "raw_output": "3 passed in 0.04s"}


def test_shape_run_tests_result_not_ok_on_nonzero_exit():
    out = shape_run_tests_result(1, "1 failed in 0.02s")
    assert out["ok"] is False
    assert out["exit_code"] == 1
    assert "1 failed" in out["raw_output"]


def test_shape_run_tests_result_preserves_raw_failure_text():
    raw = "E   assert 1 == 2\n1 failed in 0.02s"
    out = shape_run_tests_result(1, raw)
    # RAW failure assertion text must survive verbatim (no summarization).
    assert "assert 1 == 2" in out["raw_output"]


def test_shape_run_tests_result_tail_truncates_huge_output():
    raw = "x" * 20000
    out = shape_run_tests_result(0, raw)
    assert len(out["raw_output"]) == 8000
    # tail kept, not head
    assert out["raw_output"] == raw[-8000:]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_run_tests_tool.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_run_tests_command' from 'services.orchestrator.local_tools'`.

- [ ] **Step 3: Implement the helpers**

In `services/orchestrator/local_tools.py`, add the imports `os` and `shlex` at the top (next to the existing imports) and append the following after the `LOCAL_TOOL_NAMES` / constants block (do not modify any existing function):

```python
import os
import shlex

RUN_TESTS_DEFAULT_CMD = "pytest"
RUN_TESTS_TIMEOUT_MS_DEFAULT = 120000


def build_run_tests_command(args: dict[str, Any]) -> tuple[str, int]:
    """Pure builder: ReAct tool args -> (shell command string, timeout_ms).

    The base runner is LABMATE_TEST_CMD (default "pytest"); callers may scope
    the run with args["path"] (a file/dir/nodeid) and args["expr"] (a pytest
    -k expression). Timeout precedence: args["timeout_ms"] > LABMATE_TEST_TIMEOUT_MS
    > RUN_TESTS_TIMEOUT_MS_DEFAULT. No subprocess is launched here — this is a
    pure, unit-testable shaping function; execution happens via the bash seam.
    """
    base = os.getenv("LABMATE_TEST_CMD", RUN_TESTS_DEFAULT_CMD).strip() or RUN_TESTS_DEFAULT_CMD
    parts = [base]

    path = str(args.get("path") or "").strip()
    if path:
        parts.append(shlex.quote(path) if (" " in path) else path)

    expr = str(args.get("expr") or "").strip()
    if expr:
        # A multi-word -k expression ("a or b") must be a single shell arg.
        parts.append("-k")
        parts.append(shlex.quote(expr) if (" " in expr) else expr)

    command = " ".join(parts)

    timeout_ms = args.get("timeout_ms")
    if timeout_ms is None:
        env_to = os.getenv("LABMATE_TEST_TIMEOUT_MS")
        timeout_ms = int(env_to) if env_to else RUN_TESTS_TIMEOUT_MS_DEFAULT
    else:
        timeout_ms = int(timeout_ms)

    return command, timeout_ms


def shape_run_tests_result(exit_code: int, raw_output: str) -> dict[str, Any]:
    """Pure shaper: a finished test run -> {ok, exit_code, raw_output}.

    `ok` mirrors the real process exit code (0 == pass). `raw_output` is the
    RAW combined stdout/stderr, only tail-truncated to 8000 chars — NEVER
    summarized, so the model sees real pass/fail text (the failing-assertion
    lines) and cannot fabricate "all tests pass".
    """
    raw = raw_output or ""
    return {
        "ok": int(exit_code) == 0,
        "exit_code": int(exit_code),
        "raw_output": raw[-8000:],
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_run_tests_tool.py -q`
Expected: PASS (12 passed).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/local_tools.py tests/services/orchestrator/test_run_tests_tool.py
git commit -m "feat(orchestrator): pure run_tests command/result helpers"
```

---

## Task 2: write_file read-back verify helper

**Files:**
- Modify: `services/orchestrator/local_tools.py`
- Test: `tests/services/orchestrator/test_local_tools.py`

**Interfaces:**
- Consumes: nothing from prior tasks (pure string compare).
- Produces:
  - `verify_written_content(requested: str, readback: Any) -> str | None` — returns `None` when `readback` equals `requested` exactly (write verified); otherwise returns a human-readable error string starting with `"write verification failed"` and containing the phrase `"did not match"`. Tolerates a non-string `readback` (e.g. the client returned a dict/None) by treating it as a mismatch.

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/orchestrator/test_local_tools.py`:

```python
# ── write_file read-back verification ────────────────────────────────────────
from services.orchestrator.local_tools import verify_written_content  # noqa: E402


def test_verify_written_content_returns_none_on_exact_match():
    assert verify_written_content("hello\nworld\n", "hello\nworld\n") is None


def test_verify_written_content_flags_mismatch():
    err = verify_written_content("NEW CONTENT", "OLD CONTENT")
    assert err is not None
    assert err.startswith("write verification failed")
    assert "did not match" in err


def test_verify_written_content_flags_partial_write():
    err = verify_written_content("line1\nline2\n", "line1\n")
    assert err is not None
    assert "did not match" in err


def test_verify_written_content_treats_non_string_readback_as_mismatch():
    err = verify_written_content("content", {"unexpected": "shape"})
    assert err is not None
    assert "did not match" in err


def test_verify_written_content_treats_none_readback_as_mismatch():
    err = verify_written_content("content", None)
    assert err is not None
    assert "did not match" in err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_local_tools.py -q -k verify_written_content`
Expected: FAIL — `ImportError: cannot import name 'verify_written_content'`.

- [ ] **Step 3: Implement the helper**

In `services/orchestrator/local_tools.py`, append:

```python
def verify_written_content(requested: str, readback: Any) -> str | None:
    """Compare what we asked to write against what the file reads back.

    Returns None when the read-back content matches the requested content
    exactly (write confirmed applied). Otherwise returns an explicit error
    string the model will see in the tool result, so a write that silently
    did not apply (the "code was not successfully updated" failure) surfaces
    as a hard, visible error instead of a phantom success. A non-string
    read-back (dict/None/etc.) is treated as a mismatch.
    """
    if isinstance(readback, str) and readback == requested:
        return None
    got_len = len(readback) if isinstance(readback, str) else "n/a (non-text read-back)"
    return (
        "write verification failed: file content after write did not match the "
        f"content that was requested (requested {len(requested)} chars, "
        f"read back {got_len}). The write may not have applied — re-read the "
        "file and try again; do NOT report the file as updated."
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_local_tools.py -q -k verify_written_content`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/local_tools.py tests/services/orchestrator/test_local_tools.py
git commit -m "feat(orchestrator): write_file read-back verify helper"
```

---

## Task 3: run_tests tool schema in the frozen prefix

**Files:**
- Modify: `services/orchestrator/prompt_assembler.py`
- Test: `tests/services/orchestrator/test_run_tests_tool.py`

**Interfaces:**
- Consumes: nothing (schema is self-contained).
- Produces: `PromptAssembler.tools()` now includes a function tool named `run_tests` with parameters `path` (string, optional), `expr` (string, optional), `timeout_ms` (integer, optional) — no `required` keys (a bare `run_tests` runs the whole default suite). It appears in the static tail immediately before `finish`, so it is part of the byte-stable frozen prefix.

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/orchestrator/test_run_tests_tool.py`:

```python
# ── prompt assembler: run_tests is in the frozen prefix ──────────────────────
from services.orchestrator.prompt_assembler import PromptAssembler  # noqa: E402


def _tool_names(assembler: PromptAssembler) -> list[str]:
    return [t["function"]["name"] for t in assembler.tools()]


def test_run_tests_tool_present_with_no_skill_router():
    a = PromptAssembler(skill_router=None, codegraph_enabled=False)
    assert "run_tests" in _tool_names(a)


def test_run_tests_tool_present_with_codegraph():
    a = PromptAssembler(skill_router=None, codegraph_enabled=True)
    assert "run_tests" in _tool_names(a)


def test_run_tests_tool_schema_shape():
    a = PromptAssembler(skill_router=None)
    schema = next(t for t in a.tools() if t["function"]["name"] == "run_tests")
    props = schema["function"]["parameters"]["properties"]
    assert "path" in props
    assert "expr" in props
    assert "timeout_ms" in props
    # bare run_tests (whole suite) must be valid -> no required params
    assert schema["function"]["parameters"].get("required", []) == []


def test_run_tests_appears_before_finish_in_tail():
    a = PromptAssembler(skill_router=None)
    names = _tool_names(a)
    assert names.index("run_tests") < names.index("finish")


def test_prefix_is_byte_stable_with_run_tests():
    # The frozen prefix must remain deterministic (prefix-cache stability).
    a1 = PromptAssembler(skill_router=None)
    a2 = PromptAssembler(skill_router=None)
    assert a1.canonical_prefix() == a2.canonical_prefix()
    assert a1.prefix_fingerprint() == a2.prefix_fingerprint()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_run_tests_tool.py -q -k "run_tests_tool_present or run_tests_tool_schema or run_tests_appears or prefix_is_byte"`
Expected: FAIL — `run_tests` not found in tool list (`StopIteration` / assertion error).

- [ ] **Step 3: Add the schema to the static tail**

In `services/orchestrator/prompt_assembler.py`, edit `_static_tail_schemas()` to insert the `run_tests` schema between the `run_bash` entry and the `finish` entry. The `finish` dict stays last. Replace the `run_bash` + `finish` portion so it reads:

```python
        {
            "type": "function",
            "function": {
                "name": "run_bash",
                "description": "Run a bash command in the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string", "description": "Bash command"}},
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_tests",
                "description": (
                    "Run the project's REAL test suite (pytest) and return the RAW "
                    "pass/fail output. Use this to VERIFY a fix actually works — do not "
                    "claim tests pass without calling this and reading its raw_output. "
                    "Optional 'path' scopes to a file/dir/nodeid; optional 'expr' is a "
                    "pytest -k expression. Returns {ok, exit_code, raw_output}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "File, dir, or pytest nodeid to test. Omit to run the whole suite."},
                        "expr": {"type": "string", "description": "pytest -k expression to select tests, optional."},
                        "timeout_ms": {"type": "integer", "description": "Max run time in milliseconds, optional."},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finish",
                "description": "Finish the task and return the summary.",
                "parameters": {
                    "type": "object",
                    "properties": {"summary": {"type": "string", "description": "Task summary"}},
                    "required": ["summary"],
                },
            },
        },
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_run_tests_tool.py -q`
Expected: PASS (all unit tests for Tasks 1 + 3 green).

Also confirm the prefix-cache stability suite still passes (the prefix changed shape but must stay deterministic):

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q -k "prefix"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/prompt_assembler.py tests/services/orchestrator/test_run_tests_tool.py
git commit -m "feat(orchestrator): add run_tests to the frozen ReAct tool prefix"
```

---

## Task 4: Wire run_tests dispatch into _run_react_loop

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (inside `_run_react_loop`, the tool-dispatch chain ~lines 479-564)
- Test: covered by the BDD scenarios in Task 6 (no isolated unit test — this branch's behavior is a loop integration concern, exercised through `react_execute`).

**Interfaces:**
- Consumes: `build_run_tests_command`, `shape_run_tests_result` from Task 1.
- Produces: when the model emits a `run_tests` tool call, the loop runs the test command through the EXISTING `self.mcp.call_tool("exec_run", ...)` seam (same as `run_bash`), then appends `json.dumps(shape_run_tests_result(exit_code, raw_output))` as the tool `content`. The `exit_code` comes from the MCP result's `isError` flag mapped to 0/1 (matching `run_in_sandbox`'s existing convention), so a failing suite reports `ok=False`.

- [ ] **Step 1: Add the import**

In `services/orchestrator/coding_orchestrator.py`, extend the existing local_tools import (currently `from .local_tools import LOCAL_TOOL_NAMES, request_local_tool`) to:

```python
from .local_tools import (
    LOCAL_TOOL_NAMES,
    request_local_tool,
    build_run_tests_command,
    shape_run_tests_result,
)
```

- [ ] **Step 2: Add the run_tests dispatch branch**

In `_run_react_loop`, insert a new `elif` branch immediately AFTER the existing `elif name == "run_bash":` block and BEFORE the `elif name == "code_semantic_search":` block. The new branch:

```python
                    elif name == "run_tests":
                        # First-class test runner: run the REAL pytest command through
                        # the same server-side bash seam run_bash uses (sandbox rule),
                        # and hand the model the RAW pass/fail output so it cannot
                        # fabricate "all tests pass".
                        if self.mcp is not None:
                            command, timeout_ms = build_run_tests_command(args)
                            try:
                                obs = await self.mcp.call_tool(
                                    "exec_run",
                                    {
                                        "command": command,
                                        "cwd": self.workspace,
                                        "timeout": timeout_ms,
                                    },
                                )
                                raw = "\n".join(
                                    c.text for c in obs.content if hasattr(c, "text")
                                )
                                exit_code = 1 if getattr(obs, "isError", False) else 0
                                content = json.dumps(
                                    shape_run_tests_result(exit_code, raw)
                                )
                            except Exception as exc:
                                content = json.dumps({"error": str(exc)})
                        else:
                            content = json.dumps({"error": "no test runner available"})
```

- [ ] **Step 3: Verify regression suite still passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_coding_orchestrator.py tests/services/orchestrator/test_iteration_budget_bdd.py tests/services/orchestrator/test_tool_loop_detection_bdd.py -q`
Expected: PASS (existing loop behavior unchanged — `run_tests` is purely additive).

- [ ] **Step 4: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py
git commit -m "feat(orchestrator): wire run_tests tool into _run_react_loop"
```

---

## Task 5: Wire write_file read-back into the LOCAL_TOOL_NAMES branch

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (inside `_run_react_loop`, the `elif name in LOCAL_TOOL_NAMES:` block ~lines 515-527)
- Test: covered by the BDD scenarios in Task 6.

**Interfaces:**
- Consumes: `verify_written_content` from Task 2; `request_local_tool` (existing).
- Produces: when the dispatched local tool is `write_file` and the client reports success, the loop issues a follow-up `request_local_tool("read_file", {"path": <same path>})`, compares the read-back to the requested content via `verify_written_content`, and:
  - on match → `content` is `{"result": <write result>, "verified": true}`,
  - on mismatch → `content` is `{"error": "<write verification failed ...>"}` so the model sees an explicit failure.
  Non-`write_file` local tools (`read_file`, `list_dir`) keep their EXACT current behavior.

- [ ] **Step 1: Replace the LOCAL_TOOL_NAMES branch**

In `_run_react_loop`, replace the existing block:

```python
                    elif name in LOCAL_TOOL_NAMES:
                        if self.redis is not None:
                            try:
                                result = await request_local_tool(
                                    self.redis, name, args
                                )
                                content = json.dumps({"result": result}, default=str)
                            except Exception as exc:
                                content = json.dumps({"error": str(exc)})
                        else:
                            content = json.dumps(
                                {"error": "no local tool client connected"}
                            )
```

with:

```python
                    elif name in LOCAL_TOOL_NAMES:
                        if self.redis is not None:
                            try:
                                result = await request_local_tool(
                                    self.redis, name, args
                                )
                                # Reliable write: after a write_file the client may
                                # report success without the bytes landing. Read the
                                # file back and confirm it matches what we asked to
                                # write; surface an explicit error to the model on
                                # mismatch so it cannot claim "code updated" falsely.
                                if name == "write_file":
                                    requested = str(args.get("content", ""))
                                    try:
                                        readback = await request_local_tool(
                                            self.redis,
                                            "read_file",
                                            {"path": args.get("path", "")},
                                        )
                                    except Exception as exc:
                                        readback = f"<read-back failed: {exc}>"
                                    verify_err = verify_written_content(requested, readback)
                                    if verify_err is not None:
                                        content = json.dumps({"error": verify_err})
                                    else:
                                        content = json.dumps(
                                            {"result": result, "verified": True},
                                            default=str,
                                        )
                                else:
                                    content = json.dumps({"result": result}, default=str)
                            except Exception as exc:
                                content = json.dumps({"error": str(exc)})
                        else:
                            content = json.dumps(
                                {"error": "no local tool client connected"}
                            )
```

- [ ] **Step 2: Add the import**

Extend the local_tools import added in Task 4 to also include `verify_written_content`:

```python
from .local_tools import (
    LOCAL_TOOL_NAMES,
    request_local_tool,
    build_run_tests_command,
    shape_run_tests_result,
    verify_written_content,
)
```

- [ ] **Step 3: Verify regression suite still passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q`
Expected: PASS (the read-back only triggers for `write_file`; `read_file`/`list_dir` unaffected).

- [ ] **Step 4: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py
git commit -m "feat(orchestrator): write_file read-back verification in ReAct loop"
```

---

## Task 6: BDD scenarios — run_tests + write_file read-back

**Files:**
- Create: `tests/services/orchestrator/features/run_tests_tool.feature` (full content in the **Behavior (BDD) — Gherkin** section above — create it verbatim)
- Create: `tests/services/orchestrator/test_run_tests_tool_bdd.py`

**Interfaces:**
- Consumes: `AsyncOrchestrator.react_execute`, `PromptAssembler`, `run_async` (from `tests/conftest.py`). Patches the model seam at `services.orchestrator.coding_orchestrator.litellm.acompletion` (the same object `acompletion_with_failover` resolves at call time) and the local-tool seam at `services.orchestrator.coding_orchestrator.request_local_tool`.

- [ ] **Step 1: Create the feature file**

Create `tests/services/orchestrator/features/run_tests_tool.feature` with the exact Gherkin from the **Behavior (BDD) — Gherkin** section above.

- [ ] **Step 2: Write the step definitions (failing)**

Create `tests/services/orchestrator/test_run_tests_tool_bdd.py`:

```python
"""Step definitions for the run_tests tool + reliable write_file BDD feature."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from services.orchestrator.prompt_assembler import PromptAssembler
from tests.conftest import run_async

scenarios("features/run_tests_tool.feature")


# ── helpers ──────────────────────────────────────────────────────────────────

def _tool_call_msg(name: str, arguments: dict):
    tc = MagicMock()
    tc.id = f"call-{name}"
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(arguments)
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


@pytest.fixture
def ctx():
    return {
        "responses": [],
        "result": None,
        "tool_results": [],   # captured tool-message contents (role == "tool")
        "assembler": None,
    }


# ── Background ───────────────────────────────────────────────────────────────

@given("an AsyncOrchestrator with no skill router and no mcp")
def _orch_no_mcp(ctx):
    ctx["orch"] = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")


@given("an AsyncOrchestrator with no skill router and a stub bash seam")
def _orch_stub_bash(ctx):
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    ctx["orch"] = orch  # mcp set by the bash-seam step below


@given("an AsyncOrchestrator with no skill router and a local tool client")
def _orch_local_client(ctx):
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    orch.redis = MagicMock()  # presence triggers the LOCAL_TOOL_NAMES branch
    ctx["orch"] = orch


# ── Given: prompt assembler / bash seam / local client programming ───────────

@given("the prompt assembler builds the tool list")
def _build_tools(ctx):
    ctx["assembler"] = PromptAssembler(skill_router=None, codegraph_enabled=False)


@given(parsers.parse('the bash seam returns exit code {code:d} with output "{output}"'))
def _bash_returns(ctx, code, output):
    mcp = AsyncMock()
    res = MagicMock()
    res.content = [MagicMock(text=output.replace("\\n", "\n"))]
    res.isError = code != 0
    mcp.call_tool.return_value = res
    ctx["orch"].mcp = mcp


@given(parsers.parse('the write_file client reports success but the file reads back as "{readback}"'))
def _client_mismatch(ctx, readback):
    ctx["readback"] = readback
    ctx["write_ok"] = True


@given(parsers.parse('the write_file client reports success and the file reads back as "{readback}"'))
def _client_match(ctx, readback):
    ctx["readback"] = readback
    ctx["write_ok"] = True


# ── Given: scripted model turns ──────────────────────────────────────────────

def _ensure_len(ctx, turn):
    while len(ctx["responses"]) < turn:
        ctx["responses"].append(_tool_call_msg("finish", {"summary": "filler"}))


@given(parsers.parse('the model calls run_tests with path "{path}" on turn {turn:d}'))
def _run_tests_turn(ctx, path, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("run_tests", {"path": path})


@given(parsers.parse('the model calls write_file with path "{path}" and content "{content}" on turn {turn:d}'))
def _write_file_turn(ctx, path, content, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg(
        "write_file", {"path": path, "content": content}
    )


@given(parsers.parse('the model calls finish with summary "{summary}" on turn {turn:d}'))
def _finish_turn(ctx, summary, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("finish", {"summary": summary})


# ── When ─────────────────────────────────────────────────────────────────────

@when("the prompt assembler builds the tool list")
def _when_build(ctx):
    ctx["assembler"] = PromptAssembler(skill_router=None, codegraph_enabled=False)


def _capture_messages(orch, ctx):
    """Patch the loop to record tool-role message contents as they are appended."""
    # _run_react_loop appends tool results as {"role": "tool", ..., "content": ...}.
    # We capture by wrapping request_local_tool / mcp; simplest: read from the
    # returned messages is not exposed, so instead we assert via tool_results
    # gathered by a request_local_tool side_effect (write_file path) and by the
    # bash seam (run_tests path). For run_tests we read the result off the model
    # loop by intercepting json content through a patched events.emit("tool.done").


@when(parsers.parse('react_execute runs the goal "{goal}"'))
def _run_goal(ctx, goal):
    orch = ctx["orch"]

    captured: list[str] = []

    # Capture every tool.done result payload — this is the tool `content` string
    # the model would see, emitted verbatim in _run_react_loop's tool.done event.
    async def _emit(event_type, **kw):
        if event_type == "tool.done" and "result" in kw:
            captured.append(kw["result"])

    # Program the local tool client for write_file scenarios:
    #   first call  (write_file) -> success result
    #   second call (read_file)  -> the programmed read-back
    async def _local(redis, name, args, **kw):
        if name == "read_file":
            return ctx.get("readback")
        return {"ok": True}

    with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new_callable=AsyncMock, side_effect=ctx["responses"]), \
         patch("services.orchestrator.coding_orchestrator.events.emit",
               new=_emit), \
         patch("services.orchestrator.coding_orchestrator.request_local_tool",
               new=_local):
        ctx["result"] = run_async(orch.react_execute(goal))

    ctx["tool_results"] = captured


# ── Then: tool list assertions ───────────────────────────────────────────────

@then(parsers.parse('the tool list contains a tool named "{name}"'))
def _tool_list_has(ctx, name):
    names = [t["function"]["name"] for t in ctx["assembler"].tools()]
    assert name in names


@then(parsers.parse('the run_tests tool has a "{param}" parameter'))
def _run_tests_has_param(ctx, param):
    schema = next(t for t in ctx["assembler"].tools() if t["function"]["name"] == "run_tests")
    assert param in schema["function"]["parameters"]["properties"]


# ── Then: run_tests result assertions ────────────────────────────────────────

def _run_tests_payload(ctx) -> dict:
    # The run_tests tool.done result is the json string {ok, exit_code, raw_output}.
    for raw in ctx["tool_results"]:
        try:
            obj = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if "raw_output" in obj:
            return obj
    raise AssertionError(f"no run_tests payload found in {ctx['tool_results']}")


@then(parsers.parse("the tool result json has ok {value}"))
def _result_ok(ctx, value):
    assert _run_tests_payload(ctx)["ok"] is (value == "True")


@then(parsers.parse("the tool result json has exit_code {code:d}"))
def _result_exit(ctx, code):
    assert _run_tests_payload(ctx)["exit_code"] == code


@then(parsers.parse('the tool result raw_output contains "{needle}"'))
def _result_raw_contains(ctx, needle):
    assert needle in _run_tests_payload(ctx)["raw_output"]


# ── Then: write_file verification assertions ─────────────────────────────────

def _write_payload_text(ctx) -> str:
    # Concatenate all tool-result strings; the write_file branch result is among them.
    return "\n".join(ctx["tool_results"])


@then(parsers.parse('the write_file tool result contains "{needle}"'))
def _write_contains(ctx, needle):
    assert needle in _write_payload_text(ctx)


@then(parsers.parse('the write_file tool result does not contain "{needle}"'))
def _write_not_contains(ctx, needle):
    assert needle not in _write_payload_text(ctx)
```

- [ ] **Step 3: Run the BDD scenarios to verify they fail (then pass)**

First confirm the harness binds and the scenarios run:

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_run_tests_tool_bdd.py -q`
Expected after Tasks 1-5 are implemented: PASS (5 scenarios). If a scenario fails, fix the step def or the wired branch — do not weaken the assertion.

> Note for the implementer: the model seam patch target `services.orchestrator.coding_orchestrator.litellm.acompletion` works because `acompletion_with_failover` resolves `litellm.acompletion` lazily at call time against the same `litellm` module object the orchestrator imported (verified against `test_tool_loop_detection_bdd.py`, which patches the identical target). The `events.emit` patch captures the tool `content` string verbatim from the existing `tool.done` emission in `_run_react_loop`, so no production code is added just for the test.

- [ ] **Step 4: Run the full orchestrator suite (regression gate)**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q`
Expected: PASS — all prior orchestrator + memory tests still green plus the new run_tests unit/BDD tests.

- [ ] **Step 5: Commit**

```bash
git add tests/services/orchestrator/features/run_tests_tool.feature tests/services/orchestrator/test_run_tests_tool_bdd.py
git commit -m "test(orchestrator): BDD scenarios for run_tests tool + write_file read-back"
```

---

## Self-Review

**1. Spec coverage**

| Spec requirement | Task |
|---|---|
| Flat `run_tests` tool runs a REAL pytest command | Task 1 (`build_run_tests_command`) + Task 4 (dispatch via `exec_run`) |
| Returns structured `{ok, exit_code, raw_output}` with RAW output (not summary) | Task 1 (`shape_run_tests_result`, 8000-char tail, no summarization) |
| Always-available in the loop like other primitives | Task 3 (added to `_static_tail_schemas`, frozen prefix) |
| Runs via existing sandbox/run_bash path | Task 4 (reuses `self.mcp.call_tool("exec_run", ...)`) |
| write_file reads back and confirms content | Task 2 (`verify_written_content`) + Task 5 (read_file after write) |
| Explicit error string on mismatch the model sees | Task 2 (error text "write verification failed ... did not match") + Task 5 (returned as `{"error": ...}`) |
| Pure, unit-testable helpers without a live model | Task 1 + Task 2 helpers are pure functions; tested in `test_run_tests_tool.py` / `test_local_tools.py` |
| Additive + regression-safe (existing tools unchanged) | Tasks 4 & 5 add an `elif`/wrap `write_file` only; Steps 3 in both run the full suite as a regression gate |
| Env knob for test command/timeout | Task 1 (`LABMATE_TEST_CMD`, `LABMATE_TEST_TIMEOUT_MS`) |
| Honor CLAUDE.md (stdout-sacred, no tiktoken, asyncio-correct) | No `print`/stdout writes added; no tiktoken; no `asyncio.run` inside the loop; Global Constraints enforced |
| Full `.feature` Gherkin: real exit code + raw output, failing suite surfaces failure text, read-back catches non-applied write | Behavior (BDD) section + Task 6 |
| Reuse `fake_model`/seam contract (do NOT recreate) | Task 6 patches the documented `coding_orchestrator.litellm.acompletion` seam; does not redefine `fake_model` |

**2. Placeholder scan:** No "TBD"/"add error handling"/"similar to Task N". Every code step contains complete code. The `_capture_messages` stub in the BDD file is intentionally a no-op explanatory note — the actual capture is done by the `events.emit` patch in the `@when` step; it carries a comment explaining why and is never called, so it is not a placeholder for missing logic.

**3. Type consistency:** Helper names are stable across tasks — `build_run_tests_command` / `shape_run_tests_result` (Task 1) imported in Task 4; `verify_written_content` (Task 2) imported in Task 5. Return shapes match: `shape_run_tests_result` → `{ok, exit_code, raw_output}` is what Task 6's `_run_tests_payload` asserts on (`raw_output`, `ok`, `exit_code`). `verify_written_content` returns `str | None` and Task 5 branches on `is not None`. The `exec_run` MCP result handling (`isError` → 0/1, `c.text` join) matches the existing `run_bash` branch and `run_in_sandbox` convention.

**4. Seam correctness verified against current code:** `_run_react_loop` (coding_orchestrator.py lines 319-596) is the loop the harness-robustness/perf merge made canonical; `run_tests` and the `write_file` read-back are added inside it. The model-seam patch target matches the proven pattern in `test_tool_loop_detection_bdd.py`. `PromptAssembler._static_tail_schemas` is the only place that controls always-present tools, so adding `run_tests` there (not in the skill/codegraph conditional blocks) guarantees it is available regardless of skill-router/codegraph presence.
