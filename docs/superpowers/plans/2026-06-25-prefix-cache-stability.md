# Prefix-Cache Stability (llama.cpp prompt cache reuse) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Guarantee that the ReAct loop in `services/orchestrator/coding_orchestrator.py` sends a byte-identical system+tools *prefix* on every step of a single goal, so `llama-server` reuses its cached prompt prefix and only recomputes the appended tail.

**Architecture:** Extract prefix assembly into a small, pure, deterministic helper (`PromptAssembler`) that builds the system message and the tool-schema list **once per goal**, serializes them with sorted keys and stable ordering, and exposes a `prefix_fingerprint()` for testability. The ReAct loop is refactored to construct one `PromptAssembler` per `react_execute` call, reuse its frozen `system` string and `tools` list across all steps, and only ever **append** to `messages`. A pytest-bdd regression suite captures the actual request bodies of two consecutive ReAct steps (via the `fake_model` respx fixture from the foundation plan) and asserts the system+tools prefix is identical.

**Tech Stack:** Python 3.11+, `litellm.acompletion` (OpenAI-compatible client against llama.cpp `llama-server`), `pytest`, `pytest-asyncio`, `pytest-bdd`, `respx` (mocks the OpenAI HTTP endpoint), `json` (canonical serialization).

## Global Constraints

- **This is NOT Anthropic prompt caching.** Labmate serves a single local model via `llama-server` (llama.cpp). The win is llama.cpp's **longest-common-prefix prompt cache**: if the leading tokens of a prompt match the previous request, llama-server skips re-evaluating them. The contract this plan enforces is *byte-identical prefix across steps*, nothing more.
- **llama-server prompt-cache flags (Architecture / cite `research/llm-harness-research/specs/spec_inference.md`):** `spec_inference.md` documents `llama-server` as the single-box GGUF serving path (§2.2 table, §2.5 streaming, §11.2 MTP). Prompt-prefix caching is a `llama-server` runtime behavior enabled by the cache flags on the serve command — the project's serve command lives in `CLAUDE.md` Critical Rule 6 (`llama-server -m … --jinja -fa on --reasoning-format deepseek …`). The flags that make prefix reuse effective: `--ctx-size` large enough to hold the full prefix + tail, `-fa on` (flash attention; reduces KV cost of the retained prefix), and (where the build supports it) `--cache-reuse N` / slot KV reuse so the longest common prefix is matched against the prior request in the same slot. **Do not** add `--reasoning-budget N` as a server flag (CLAUDE.md Rule 6) — per-request `thinking_budget_tokens` must stay live. This plan changes **only** the client-side prefix assembly; no serve-command change is required for correctness, but prefix stability is the *precondition* for any of those flags to help.
- **Behavior-preserving refactor.** The assembled `system` string and `tools` list must be **semantically identical** to the current inline construction in `react_execute` (lines ~266–410 of `coding_orchestrator.py`). Same prompt text, same tool set, same tool schemas. No tool added or removed. Existing `test_coding_orchestrator.py` tests must continue to pass unchanged.
- **Determinism rules for the prefix** (every task implicitly requires these):
  - No `time`, `datetime`, `now_iso()`, `uuid`, or `random` anywhere in the prefix (system string or tools list).
  - Tool ordering is fixed and explicit (a list, never a `set`).
  - When the assembler controls JSON serialization, use `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
  - `messages` is **append-only**: never mutate, reorder, or delete an existing entry. Index 0 (system) and the tools argument are frozen for the life of a goal.
- **File/naming conventions** (CLAUDE.md): Python files `snake_case.py`, classes `PascalCase`, functions `snake_case`. Mark mocked tests `@pytest.mark.mocked`; this feature has no `@pytest.mark.live` tests.
- **Do NOT modify** `core/`, `tools/`, or `main.py` (M2 baseline). All work is under `services/orchestrator/` and `tests/services/orchestrator/`.
- **Shared BDD contract (from the foundation plan):** `tests/conftest.py` already provides a `pytest-bdd` setup and a `fake_model` respx fixture that mocks the OpenAI-compatible `/v1/chat/completions` endpoint and records request bodies. This plan consumes that fixture; it does not redefine it.

---

## Behavior (BDD) — Gherkin

Full feature file (created in Task 4):

```gherkin
# tests/services/orchestrator/features/prefix_cache_stability.feature
@mocked
Feature: Prefix-cache stability across ReAct steps
  llama-server caches the longest common prefix of a prompt. To benefit, Labmate
  must send a byte-identical system+tools prefix on every step of one goal, and
  only ever append new messages. This feature locks that contract in place.

  Background:
    Given a fake OpenAI-compatible model that records every request body
    And an AsyncOrchestrator with no skill router and no MCP bridge

  Scenario: Two consecutive ReAct steps share a byte-identical system+tools prefix
    Given the model is scripted to call run_bash on step 1 then finish on step 2
    When react_execute runs the goal "inspect the repo then finish"
    Then the model received at least 2 requests
    And the system message of request 2 equals the system message of request 1
    And the tools list of request 2 equals the tools list of request 1
    And the serialized system+tools prefix of request 2 is byte-identical to request 1

  Scenario: Appended messages do not alter the prefix
    Given the model is scripted to call run_bash on step 1 then finish on step 2
    When react_execute runs the goal "inspect the repo then finish"
    Then request 2 has strictly more messages than request 1
    And the messages of request 1 are a prefix of the messages of request 2
    And the first message of every request is the identical system message

  Scenario: Tool ordering is stable across independent runs
    Given a second AsyncOrchestrator built with the same configuration
    When react_execute runs the goal "do nothing then finish" on each orchestrator
    Then the tools list sent by both orchestrators is identical in order and content
    And the serialized system+tools prefix is byte-identical between the two runs
```

---

## File Map

| File | Responsibility |
|------|----------------|
| `services/orchestrator/prompt_assembler.py` | **New.** Pure `PromptAssembler` class. Builds the frozen `system` string and `tools` list **once** from the orchestrator's capabilities (skill catalog/schema, codegraph flag), exposes `system_message()`, `tools()`, `canonical_prefix()` (deterministic JSON string of system+tools), and `prefix_fingerprint()` (sha256 of the canonical prefix). No I/O, no time, no randomness. |
| `services/orchestrator/coding_orchestrator.py` | **Modify.** In `react_execute` (lines ~266–410), replace the inline `tools=[...]` build and inline `system = (...)` string with one `PromptAssembler` instance built before the loop. Freeze `system_msg = assembler.system_message()` and `tools = assembler.tools()`; pass the **same objects** to `litellm.acompletion` every step. Keep `messages` append-only (already true at lines 461, 604 — preserve). |
| `tests/services/orchestrator/test_prompt_assembler.py` | **New.** Unit TDD for `PromptAssembler`: determinism (same inputs → byte-identical `canonical_prefix()`), stable tool order, no nondeterministic fields, semantic parity of the system text and tool set with the old inline values, behavior under skill-router / codegraph presence. |
| `tests/services/orchestrator/features/prefix_cache_stability.feature` | **New.** The Gherkin above. |
| `tests/services/orchestrator/test_prefix_cache_stability_bdd.py` | **New.** pytest-bdd step definitions. Drives `react_execute` against the `fake_model` respx fixture, captures consecutive request bodies, asserts prefix identity. |

---

### Task 1: `PromptAssembler` core — frozen system + tools, deterministic prefix

**Files:**
- Create: `services/orchestrator/prompt_assembler.py`
- Test: `tests/services/orchestrator/test_prompt_assembler.py`

**Interfaces:**
- Consumes: nothing from earlier tasks. Reads only plain inputs passed to its constructor.
- Produces (relied on by Tasks 2–5):
  - `PromptAssembler(skill_router=None, codegraph_enabled: bool = False, base_system: str | None = None, catalog: str | None = None)` — constructor; `catalog` overrides whatever `skill_router.runner.catalog_prompt()` would return when supplied (keeps the class pure/testable without a live runner).
  - `PromptAssembler.system_message() -> dict` — returns the same `dict` object every call: `{"role": "system", "content": <str>}`.
  - `PromptAssembler.tools() -> list[dict]` — returns the same `list` object every call; stable order.
  - `PromptAssembler.canonical_prefix() -> str` — deterministic JSON string of `{"system": <system content str>, "tools": <tools list>}` via `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
  - `PromptAssembler.prefix_fingerprint() -> str` — `hashlib.sha256(canonical_prefix().encode("utf-8")).hexdigest()`.
  - Module constant `BASE_SYSTEM_PROMPT: str` — the exact execution-agent system text currently inlined in `react_execute`.

- [ ] **Step 1: Write the failing test (determinism + fingerprint stability)**

```python
# tests/services/orchestrator/test_prompt_assembler.py
from __future__ import annotations
import json
import pytest
from services.orchestrator.prompt_assembler import PromptAssembler, BASE_SYSTEM_PROMPT


@pytest.mark.mocked
def test_canonical_prefix_is_byte_identical_across_two_instances():
    a = PromptAssembler(skill_router=None, codegraph_enabled=False)
    b = PromptAssembler(skill_router=None, codegraph_enabled=False)
    assert a.canonical_prefix() == b.canonical_prefix()
    assert a.prefix_fingerprint() == b.prefix_fingerprint()


@pytest.mark.mocked
def test_system_message_and_tools_return_same_object_each_call():
    a = PromptAssembler(skill_router=None, codegraph_enabled=False)
    assert a.system_message() is a.system_message()      # frozen object reuse
    assert a.tools() is a.tools()


@pytest.mark.mocked
def test_canonical_prefix_uses_sorted_keys_and_is_valid_json():
    a = PromptAssembler(skill_router=None, codegraph_enabled=False)
    prefix = a.canonical_prefix()
    parsed = json.loads(prefix)                            # must be valid JSON
    assert set(parsed.keys()) == {"system", "tools"}
    # sort_keys means re-dumping with the same options is a fixed point
    redump = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert redump == prefix
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/orchestrator/test_prompt_assembler.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.orchestrator.prompt_assembler'`

- [ ] **Step 3: Write the minimal implementation**

```python
# services/orchestrator/prompt_assembler.py
from __future__ import annotations

import hashlib
import json
from typing import Any

# Exact execution-agent system text, copied verbatim from the old inline
# build in coding_orchestrator.react_execute. Behavior-preserving: do not edit
# wording when moving it here.
BASE_SYSTEM_PROMPT = (
    "You are an execution agent with access to specialized SKILLS plus a generic shell. "
    "CRITICAL RULE: if ANY available skill matches the task, you MUST accomplish it with "
    "that skill — call load_skill(name) to read its instructions, then "
    "call_skill_tool(skill, tool, arguments) to run the right tool. Do NOT use run_bash to "
    "hand-replicate what a skill already does (e.g. do not grep/sed/write files yourself "
    "when a code-search, test-generation, parsing, audit, or documentation skill exists). "
    "Use run_bash ONLY when no available skill fits the task. "
    "Do NOT call finish until the work is actually done — and when a matching skill exists, "
    "finish only AFTER call_skill_tool has returned its result. Call finish(summary) to end. "
    "SANDBOX RULE: run_bash is for read-only inspection (ls, cat, grep, git status) only. "
    "Any code you author or execute — Python, Node, shell scripts, pytest — MUST go through "
    "the code-sandbox skill (load_skill('code-sandbox') then call_skill_tool), NEVER run_bash."
)


def _call_skill_tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "call_skill_tool",
            "description": "Execute a tool within a loaded skill.",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill": {"type": "string", "description": "Skill name"},
                    "tool": {"type": "string", "description": "Tool name"},
                    "arguments": {"type": "object", "description": "Tool arguments"},
                },
                "required": ["skill", "tool", "arguments"],
            },
        },
    }


def _code_semantic_search_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "code_semantic_search",
            "description": (
                "Search the codebase by meaning. Returns the top-k symbols "
                "(functions, classes, methods) most semantically relevant to the query. "
                "Use when you need to find code by what it DOES rather than what it's named."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language description of what to find"},
                    "k": {"type": "integer", "description": "Number of results (max 20)", "default": 8},
                },
                "required": ["query"],
            },
        },
    }


def _static_tail_schemas() -> list[dict]:
    # read_file, write_file, list_dir, run_bash, finish — always present, fixed order.
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a UTF-8 text file from the user's local workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Workspace-relative file path"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write (create or overwrite) a UTF-8 text file in the user's local workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Workspace-relative file path"},
                        "content": {"type": "string", "description": "Full file contents to write"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_dir",
                "description": "List entries of a directory in the user's local workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Workspace-relative directory path"}},
                    "required": ["path"],
                },
            },
        },
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
                "name": "finish",
                "description": "Finish the task and return the summary.",
                "parameters": {
                    "type": "object",
                    "properties": {"summary": {"type": "string", "description": "Task summary"}},
                    "required": ["summary"],
                },
            },
        },
    ]


class PromptAssembler:
    """
    Pure, deterministic builder for the ReAct prefix (system message + tools list).

    Built ONCE per goal. The same frozen system dict and tools list are reused on
    every ReAct step so llama-server's longest-common-prefix prompt cache hits.

    No time, uuid, or randomness ever enters the prefix. Tool order is fixed and
    explicit. canonical_prefix() serializes with sorted keys for byte-stable diffs.
    """

    def __init__(
        self,
        skill_router: Any = None,
        codegraph_enabled: bool = False,
        base_system: str | None = None,
        catalog: str | None = None,
    ) -> None:
        # Resolve the skill catalog deterministically: explicit catalog wins;
        # otherwise pull it from the runner if a skill_router is present.
        if catalog is None and skill_router is not None:
            try:
                catalog = skill_router.runner.catalog_prompt()
            except Exception:
                catalog = None

        system_text = base_system if base_system is not None else BASE_SYSTEM_PROMPT
        if catalog:
            system_text = f"{system_text}\n\n{catalog}"
        self._system_msg: dict = {"role": "system", "content": system_text}

        tools: list[dict] = []
        if skill_router is not None:
            tools.append(skill_router.runner.tool_schema())   # load_skill schema
            tools.append(_call_skill_tool_schema())
        if codegraph_enabled:
            tools.append(_code_semantic_search_schema())
        tools.extend(_static_tail_schemas())
        self._tools: list[dict] = tools

    def system_message(self) -> dict:
        return self._system_msg

    def tools(self) -> list[dict]:
        return self._tools

    def canonical_prefix(self) -> str:
        payload = {"system": self._system_msg["content"], "tools": self._tools}
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def prefix_fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_prefix().encode("utf-8")).hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/orchestrator/test_prompt_assembler.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/prompt_assembler.py tests/services/orchestrator/test_prompt_assembler.py
git commit -m "feat(orchestrator): add deterministic PromptAssembler for prefix-cache stability"
```

---

### Task 2: `PromptAssembler` — semantic parity with the old inline build

**Files:**
- Modify: `tests/services/orchestrator/test_prompt_assembler.py` (append tests)
- Modify (only if a parity gap is found): `services/orchestrator/prompt_assembler.py`

**Interfaces:**
- Consumes: `PromptAssembler`, `BASE_SYSTEM_PROMPT` from Task 1.
- Produces: a verified guarantee that the assembler's `system_message()["content"]` and `tools()` match the previously-inlined values, so Task 3's refactor is behavior-preserving.

- [ ] **Step 1: Write the failing test (tool set + order + system parity)**

```python
# tests/services/orchestrator/test_prompt_assembler.py  (append)
from unittest.mock import MagicMock


@pytest.mark.mocked
def test_no_skill_router_tool_names_and_order():
    a = PromptAssembler(skill_router=None, codegraph_enabled=False)
    names = [t["function"]["name"] for t in a.tools()]
    assert names == ["read_file", "write_file", "list_dir", "run_bash", "finish"]


@pytest.mark.mocked
def test_skill_router_prepends_load_skill_and_call_skill_tool():
    runner = MagicMock()
    runner.tool_schema.return_value = {
        "type": "function",
        "function": {"name": "load_skill", "parameters": {}},
    }
    runner.catalog_prompt.return_value = "- test-skill: A test skill"
    sr = MagicMock()
    sr.runner = runner
    a = PromptAssembler(skill_router=sr, codegraph_enabled=False)
    names = [t["function"]["name"] for t in a.tools()]
    assert names == [
        "load_skill", "call_skill_tool",
        "read_file", "write_file", "list_dir", "run_bash", "finish",
    ]
    # catalog is appended to the system content (progressive disclosure)
    assert "test-skill" in a.system_message()["content"]


@pytest.mark.mocked
def test_codegraph_enabled_inserts_semantic_search_before_static_tail():
    a = PromptAssembler(skill_router=None, codegraph_enabled=True)
    names = [t["function"]["name"] for t in a.tools()]
    assert names == [
        "code_semantic_search",
        "read_file", "write_file", "list_dir", "run_bash", "finish",
    ]


@pytest.mark.mocked
def test_base_system_prompt_directs_code_to_sandbox():
    # Mirrors test_react_system_prompt_directs_code_to_sandbox parity assertions.
    a = PromptAssembler(skill_router=None)
    content = a.system_message()["content"]
    assert "code-sandbox" in content
    assert "run_bash" in content


@pytest.mark.mocked
def test_no_nondeterministic_tokens_in_prefix():
    # Guard: the canonical prefix must contain no time/uuid/random markers.
    import re
    a = PromptAssembler(skill_router=None, codegraph_enabled=True)
    prefix = a.canonical_prefix()
    # ISO timestamp fragment, e.g. 2026-06-25T or a 32-hex uuid would be a leak.
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", prefix)
    assert not re.search(r"[0-9a-f]{32}", prefix)
```

- [ ] **Step 2: Run test to verify it fails (or passes if parity already holds)**

Run: `pytest tests/services/orchestrator/test_prompt_assembler.py -v`
Expected: PASS for all five new tests. If any FAIL, the assembler diverged from the inline build — fix `prompt_assembler.py` so the tool order / system text matches `coding_orchestrator.react_execute` (lines ~266–410) exactly, then re-run.

> Rationale for "expected PASS": the Task 1 implementation was copied verbatim from the inline build, so these parity tests should pass immediately. They exist to *lock* parity; treat any failure as a real regression in Task 1.

- [ ] **Step 3: (Only if a test failed) align the assembler**

No code change is expected. If `test_skill_router_prepends_load_skill_and_call_skill_tool` fails because `tool_schema()` isn't being placed first, verify the constructor appends `skill_router.runner.tool_schema()` *before* `_call_skill_tool_schema()` (Task 1 Step 3 already does this).

- [ ] **Step 4: Run the full assembler suite**

Run: `pytest tests/services/orchestrator/test_prompt_assembler.py -v`
Expected: PASS (8 passed total)

- [ ] **Step 5: Commit**

```bash
git add tests/services/orchestrator/test_prompt_assembler.py services/orchestrator/prompt_assembler.py
git commit -m "test(orchestrator): lock PromptAssembler parity with inline ReAct prefix"
```

---

### Task 3: Refactor `react_execute` to use `PromptAssembler` (behavior-preserving)

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (the `tools=[...]`, `system=(...)`, and `messages=[...]` build inside `react_execute`, currently lines ~266–410)
- Test: existing `tests/services/orchestrator/test_coding_orchestrator.py` (no edits — must still pass), plus the assembler tests from Tasks 1–2

**Interfaces:**
- Consumes: `PromptAssembler` (Task 1).
- Produces: a `react_execute` whose per-step `litellm.acompletion` call passes the **same** `system_msg` (as `messages[0]`) and the **same** `tools` list object on every step.

- [ ] **Step 1: Add a focused failing test asserting the loop reuses one assembler**

```python
# tests/services/orchestrator/test_coding_orchestrator.py  (append)
@pytest.mark.asyncio
async def test_react_execute_builds_prompt_assembler_once_per_goal():
    """The ReAct loop constructs exactly one PromptAssembler per react_execute call."""
    from services.orchestrator.coding_orchestrator import AsyncOrchestrator
    orch = AsyncOrchestrator(skill_router=None, mcp=None, max_steps=3)

    # Step 1: run_bash (no mcp -> returns error dict, loop continues). Step 2: finish.
    r1 = _msg_with_tool_call("run_bash", '{"command":"ls"}')
    r2 = _msg_with_tool_call("finish", '{"summary":"done"}')
    resp1 = MagicMock(choices=[MagicMock(message=r1)])
    resp2 = MagicMock(choices=[MagicMock(message=r2)])

    with patch("services.orchestrator.coding_orchestrator.PromptAssembler") as MockPA:
        instance = MockPA.return_value
        instance.system_message.return_value = {"role": "system", "content": "SYS"}
        instance.tools.return_value = [{"type": "function", "function": {"name": "finish"}}]
        with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
                   new_callable=AsyncMock, side_effect=[resp1, resp2]):
            out = await orch.react_execute("inspect then finish")

    assert out["ok"] is True
    # Exactly one assembler for the whole goal — not one per step.
    assert MockPA.call_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/orchestrator/test_coding_orchestrator.py::test_react_execute_builds_prompt_assembler_once_per_goal -v`
Expected: FAIL with `AttributeError: <module 'services.orchestrator.coding_orchestrator'> does not have the attribute 'PromptAssembler'` (the name isn't imported/used yet).

- [ ] **Step 3: Refactor `react_execute` to use the assembler**

Add the import near the top of `services/orchestrator/coding_orchestrator.py` (next to `from . import events`):

```python
from .prompt_assembler import PromptAssembler
```

Then, inside `react_execute`, **delete** the inline `tools = []` … `tools.extend([...])` block (lines ~266–383), the inline `catalog = ...` / `system = (...)` block (lines ~385–405), and the inline `messages = [...]` (lines ~407–410). **Replace** all of that with:

```python
        # Build the prefix ONCE per goal. The same frozen system message and tools
        # list are reused on every ReAct step below, so llama-server's longest-common-
        # prefix prompt cache hits and only the appended tail is recomputed.
        assembler = PromptAssembler(
            skill_router=self.skill_router,
            codegraph_enabled=self.codegraph_mcp is not None,
        )
        tools = assembler.tools()                 # frozen list — never rebuilt per step
        messages = [
            assembler.system_message(),           # frozen system dict at index 0
            {"role": "user", "content": goal},
        ]
```

Leave the ReAct loop (the `for step in range(self.max_steps):` block) unchanged: it already passes `messages=messages, tools=tools` to `litellm.acompletion` and only ever **appends** to `messages` (assistant turn at the existing `messages.append(msg_dict)`, tool results at the existing `messages.append({"role": "tool", ...})`). Do not touch those appends.

- [ ] **Step 4: Run the new test + the full orchestrator suite**

Run: `pytest tests/services/orchestrator/test_coding_orchestrator.py -v`
Expected: PASS — the new `test_react_execute_builds_prompt_assembler_once_per_goal` passes, and **all pre-existing tests still pass**, including `test_react_system_prompt_directs_code_to_sandbox` (it reads `react_execute` source via `inspect.getsource`; `code-sandbox` and `run_bash` now appear because the import line and assembler call reference them — verify this assertion still holds; if it fails, see the note below).

> **Note on `test_react_system_prompt_directs_code_to_sandbox`:** that test asserts the *source of `react_execute`* contains the literals `"code-sandbox"` and `"run_bash"`. After the refactor those literals move into `prompt_assembler.py`, so this assertion may break. If it FAILS, that is a *known consequence of the move*, not a behavior regression. Fix it in Step 5 by repointing the test at the assembler (the system text now lives there).

- [ ] **Step 5: Repoint the source-inspection test to the assembler (only if it failed in Step 4)**

```python
# tests/services/orchestrator/test_coding_orchestrator.py
# Replace the body of test_react_system_prompt_directs_code_to_sandbox with:
def test_react_system_prompt_directs_code_to_sandbox():
    from services.orchestrator.prompt_assembler import BASE_SYSTEM_PROMPT
    assert "code-sandbox" in BASE_SYSTEM_PROMPT
    assert "run_bash" in BASE_SYSTEM_PROMPT
```

Run: `pytest tests/services/orchestrator/test_coding_orchestrator.py -v`
Expected: PASS (full suite green)

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_coding_orchestrator.py
git commit -m "refactor(orchestrator): assemble ReAct prefix once per goal via PromptAssembler"
```

---

### Task 4: BDD feature file + step skeleton wired to the `fake_model` fixture

**Files:**
- Create: `tests/services/orchestrator/features/prefix_cache_stability.feature`
- Create: `tests/services/orchestrator/test_prefix_cache_stability_bdd.py`

**Interfaces:**
- Consumes: the `fake_model` respx fixture from `tests/conftest.py` (foundation plan). Assumed contract: it patches the OpenAI-compatible `/v1/chat/completions` route, lets the test script per-step responses, and exposes the **recorded request bodies** (parsed JSON dicts, in order). The step defs read those bodies via a helper `recorded_request_bodies(fake_model)` defined in this file so the suite degrades gracefully if the fixture's accessor name differs.
- Produces: passing BDD scenarios that prove prefix identity across steps.

- [ ] **Step 1: Create the feature file**

Write `tests/services/orchestrator/features/prefix_cache_stability.feature` with the **exact** Gherkin from the "Behavior (BDD) — Gherkin" section above (all three scenarios, `@mocked` tag, `Background`).

- [ ] **Step 2: Write the step-def module with shared fixtures and helpers (failing — scenarios not bound yet)**

```python
# tests/services/orchestrator/test_prefix_cache_stability_bdd.py
from __future__ import annotations
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.coding_orchestrator import AsyncOrchestrator

scenarios("features/prefix_cache_stability.feature")


# ── helpers ────────────────────────────────────────────────────────────────
def _tool_call_msg(name: str, arguments_json: str):
    tc = MagicMock()
    tc.id = "call-" + name
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = arguments_json
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


def _prefix_of(body: dict) -> str:
    """Canonical system+tools prefix string for one recorded request body."""
    system = next(m for m in body["messages"] if m["role"] == "system")
    payload = {"system": system["content"], "tools": body.get("tools", [])}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@pytest.fixture
def captured():
    """Mutable bag the steps share across the scenario."""
    return {"bodies": [], "bodies2": []}


@pytest.fixture
def scripted_completion():
    """
    Patch litellm.acompletion to (a) record the request body it was called with
    and (b) return scripted responses: run_bash on step 1, finish on step 2.
    We capture the body directly here so the BDD suite does not depend on the
    fake_model fixture's internal request-log accessor name.
    """
    responses = [
        _tool_call_msg("run_bash", '{"command":"ls"}'),
        _tool_call_msg("finish", '{"summary":"done"}'),
    ]
    bodies: list[dict] = []

    async def _fake_acompletion(*args, **kwargs):
        # Record a JSON-safe snapshot of the prefix-relevant fields.
        bodies.append({
            "messages": [dict(m) for m in kwargs["messages"]],
            "tools": kwargs.get("tools", []),
        })
        return responses[min(len(bodies) - 1, len(responses) - 1)]

    with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new=AsyncMock(side_effect=_fake_acompletion)):
        yield bodies
```

- [ ] **Step 3: Run to verify the scenarios are collected but fail (unbound steps)**

Run: `pytest tests/services/orchestrator/test_prefix_cache_stability_bdd.py -v`
Expected: FAIL — pytest-bdd reports `StepDefinitionNotFoundError` for the `Given/When/Then` lines (steps not yet implemented).

- [ ] **Step 4: Implement the Given/When/Then step definitions**

```python
# tests/services/orchestrator/test_prefix_cache_stability_bdd.py  (append)

# ── Background ───────────────────────────────────────────────────────────────
@given("a fake OpenAI-compatible model that records every request body")
def _fake_model_recording(scripted_completion):
    # scripted_completion patches litellm.acompletion and yields the bodies list.
    return scripted_completion


@given("an AsyncOrchestrator with no skill router and no MCP bridge", target_fixture="orch")
def _orch():
    return AsyncOrchestrator(skill_router=None, mcp=None, max_steps=3)


# ── Scenario 1 & 2 shared driver ─────────────────────────────────────────────
@given("the model is scripted to call run_bash on step 1 then finish on step 2")
def _scripted(scripted_completion):
    return scripted_completion


@when(parsers.parse('react_execute runs the goal "{goal}"'), target_fixture="run_bodies")
@pytest.mark.asyncio
async def _run(orch, scripted_completion, goal):
    out = await orch.react_execute(goal)
    assert out["ok"] is True
    return scripted_completion          # the recorded bodies list


@then("the model received at least 2 requests")
def _at_least_two(run_bodies):
    assert len(run_bodies) >= 2


@then("the system message of request 2 equals the system message of request 1")
def _system_equal(run_bodies):
    sys1 = next(m for m in run_bodies[0]["messages"] if m["role"] == "system")
    sys2 = next(m for m in run_bodies[1]["messages"] if m["role"] == "system")
    assert sys1 == sys2


@then("the tools list of request 2 equals the tools list of request 1")
def _tools_equal(run_bodies):
    assert run_bodies[0]["tools"] == run_bodies[1]["tools"]


@then("the serialized system+tools prefix of request 2 is byte-identical to request 1")
def _prefix_identical(run_bodies):
    assert _prefix_of(run_bodies[0]) == _prefix_of(run_bodies[1])


# ── Scenario 2: append-only ──────────────────────────────────────────────────
@then("request 2 has strictly more messages than request 1")
def _more_messages(run_bodies):
    assert len(run_bodies[1]["messages"]) > len(run_bodies[0]["messages"])


@then("the messages of request 1 are a prefix of the messages of request 2")
def _messages_are_prefix(run_bodies):
    m1 = run_bodies[0]["messages"]
    m2 = run_bodies[1]["messages"]
    assert m2[: len(m1)] == m1


@then("the first message of every request is the identical system message")
def _first_is_system(run_bodies):
    first0 = run_bodies[0]["messages"][0]
    assert first0["role"] == "system"
    for b in run_bodies:
        assert b["messages"][0] == first0


# ── Scenario 3: cross-run stability ──────────────────────────────────────────
@given("a second AsyncOrchestrator built with the same configuration", target_fixture="orch2")
def _orch2():
    return AsyncOrchestrator(skill_router=None, mcp=None, max_steps=3)


@when(parsers.parse('react_execute runs the goal "{goal}" on each orchestrator'),
      target_fixture="two_run_bodies")
@pytest.mark.asyncio
async def _run_two(orch, orch2, goal):
    # Independent recordings for each orchestrator so order can't bleed.
    bodies_a: list[dict] = []
    bodies_b: list[dict] = []

    def _make(recorder):
        responses = [
            _tool_call_msg("run_bash", '{"command":"ls"}'),
            _tool_call_msg("finish", '{"summary":"done"}'),
        ]
        async def _fake(*a, **k):
            recorder.append({"messages": [dict(m) for m in k["messages"]],
                             "tools": k.get("tools", [])})
            return responses[min(len(recorder) - 1, len(responses) - 1)]
        return _fake

    with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new=AsyncMock(side_effect=_make(bodies_a))):
        await orch.react_execute(goal)
    with patch("services.orchestrator.coding_orchestrator.litellm.acompletion",
               new=AsyncMock(side_effect=_make(bodies_b))):
        await orch2.react_execute(goal)
    return bodies_a, bodies_b


@then("the tools list sent by both orchestrators is identical in order and content")
def _tools_cross_run(two_run_bodies):
    a, b = two_run_bodies
    assert a[0]["tools"] == b[0]["tools"]


@then("the serialized system+tools prefix is byte-identical between the two runs")
def _prefix_cross_run(two_run_bodies):
    a, b = two_run_bodies
    assert _prefix_of(a[0]) == _prefix_of(b[0])
```

- [ ] **Step 5: Run the BDD suite to verify it passes**

Run: `pytest tests/services/orchestrator/test_prefix_cache_stability_bdd.py -v`
Expected: PASS — all three scenarios green.

- [ ] **Step 6: Commit**

```bash
git add tests/services/orchestrator/features/prefix_cache_stability.feature \
        tests/services/orchestrator/test_prefix_cache_stability_bdd.py
git commit -m "test(orchestrator): BDD regression for byte-identical ReAct prefix across steps"
```

---

### Task 5: Full-suite regression gate + plan self-review

**Files:**
- No new files. Validation only.

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: a green run proving the refactor is behavior-preserving and the prefix contract holds.

- [ ] **Step 1: Run the assembler + BDD + orchestrator suites together**

Run:
```bash
pytest tests/services/orchestrator/test_prompt_assembler.py \
       tests/services/orchestrator/test_prefix_cache_stability_bdd.py \
       tests/services/orchestrator/test_coding_orchestrator.py -v
```
Expected: PASS — every test green; no `@pytest.mark.live` selected, no GPU needed.

- [ ] **Step 2: Run the whole orchestrator test directory (catch collateral breakage)**

Run: `pytest tests/services/orchestrator/ -q`
Expected: PASS (or unchanged from the pre-task baseline — record any pre-existing failures *before* Task 1 and confirm the count did not grow).

- [ ] **Step 3: Grep the prefix path for nondeterminism leaks**

Run:
```bash
grep -nE "uuid|time\.|datetime|now_iso|random" services/orchestrator/prompt_assembler.py
```
Expected: **no output.** Any hit means a nondeterministic token can enter the prefix — remove it before completing.

- [ ] **Step 4: Commit (no-op if nothing changed; otherwise commit fixes)**

```bash
git add -A
git commit -m "test(orchestrator): full-suite regression gate for prefix-cache stability" || echo "nothing to commit"
```

---

## Self-Review

**1. Spec coverage** (against the feature requirements):
- (a) *Build system prompt + tools once per goal, reuse exact object/string across steps* → Task 1 (`PromptAssembler` returns the same frozen `dict`/`list` objects) + Task 3 (loop builds one assembler before the `for step` loop; passes the same `tools`/`messages[0]` every step) + Task 3 Step 1 test (`MockPA.call_count == 1`).
- (b) *Deterministic serialization — sorted keys, stable tool order, no Date/now()/random* → `canonical_prefix()` uses `sort_keys=True`; `tools()` is an explicit ordered list; Task 2 `test_no_nondeterministic_tokens_in_prefix` + Task 5 Step 3 grep gate.
- (c) *Only ever append to messages* → Task 3 preserves the existing append-only `messages.append(...)` calls; Task 4 Scenario 2 asserts request 1's messages are a prefix of request 2's.
- *Focused, unit-testable helper* → `PromptAssembler` (Task 1), fully unit-tested (Tasks 1–2).
- *Refactor the ReAct loop to use it* → Task 3.
- *Regression test: serialized prefix byte-identical across two consecutive steps* → Task 4 Scenario 1 + `test_prefix_cache_stability_bdd.py::_prefix_identical`.
- *Document llama-server prompt-cache flags citing spec_inference.md* → Global Constraints (cites §2.2/§2.5/§11.2 of `spec_inference.md` + CLAUDE.md Rule 6 serve command).
- *Three Gherkin scenarios (consecutive-step identity, stable tool order across runs, appended messages don't alter prefix)* → all three present in the feature file.

**2. Placeholder scan:** No "TBD/TODO/handle edge cases/similar to Task N" — every code step shows full code. The one "expected PASS on a failing-test step" (Task 2 Step 2) is explained (parity locks, not new behavior) and Task 3 Step 5 is gated on an explicit "only if it failed" condition with the exact replacement code.

**3. Type consistency:** `PromptAssembler(skill_router, codegraph_enabled, base_system, catalog)`, `.system_message() -> dict`, `.tools() -> list[dict]`, `.canonical_prefix() -> str`, `.prefix_fingerprint() -> str`, and `BASE_SYSTEM_PROMPT: str` are used identically across Tasks 1, 2, 3, 4. The orchestrator import (`from .prompt_assembler import PromptAssembler`) matches the patch target in Task 3's test (`services.orchestrator.coding_orchestrator.PromptAssembler`) and Task 4's `_prefix_of` mirrors `canonical_prefix()`'s serialization exactly. `codegraph_enabled=self.codegraph_mcp is not None` matches the original inline guard at line 289.

One risk flagged for the implementer: if litellm mutates/normalizes the `tools` list or `messages` before the HTTP send, the *recorded* body in Task 4 (captured at the `acompletion` boundary, before litellm) is the right place to assert — the contract is "what Labmate hands to the client is identical," which is sufficient for llama-server prefix reuse since litellm's transform is itself deterministic given identical input.
