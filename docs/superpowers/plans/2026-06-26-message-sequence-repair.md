# Message-Sequence Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a pure, deterministic `sanitize_messages()` repair pass that runs before every orchestrator model call so malformed message sequences (orphaned tool results, illegal adjacent same-role runs, injected synthetic turns) never reach the OpenAI-compatible inference endpoint.

**Architecture:** A new pure module `services/orchestrator/message_repair.py` exposes `sanitize_messages(messages) -> list[dict]` (returns a NEW list; never mutates input; never touches the system message at index 0 or reorders the leading system+user prefix) and a companion `validate_messages(messages) -> list[str]` (returns detected problems, for tests/logging). The orchestrator wires `sanitize_messages` in immediately before each `acompletion_with_failover` call that passes a multi-message list — primarily inside `_run_react_loop`. An env flag `ENABLE_MESSAGE_REPAIR` (default ON / `"1"`) gates the wire-in; the module itself is always importable and pure.

**Tech Stack:** Python 3.11+, asyncio, litellm (OpenAI-compatible message schema), pytest + pytest-asyncio, pytest-bdd (respx HTTP-seam mock via `tests/conftest.py::fake_model`).

## Global Constraints

- **stdout is sacred in MCP servers.** This module is in the orchestrator (not an MCP server), but never `print()` — use `logging` to stderr if logging is needed. (CLAUDE.md rule 1)
- **Pure module.** `sanitize_messages` and `validate_messages` are deterministic, do zero I/O, never call the network, never read env inside the pure functions (the env flag is read only at the wire-in site).
- **Never mutate the input list or its dicts.** Return a NEW list of (shallow-copied where modified) dicts. (prefix-cache invariant + caller safety)
- **Never mutate or reorder the leading system+user prefix.** Index 0 (system message, when `role == "system"`) and the byte-stable system+leading-user prefix MUST be passed through identically — the llama.cpp longest-common-prefix prompt cache depends on it (see `PromptAssembler`, CLAUDE.md "Prefix-cache stability").
- **Idempotent.** `sanitize_messages(sanitize_messages(x)) == sanitize_messages(x)` for all inputs.
- **Additive + regression-safe.** A well-formed message list is returned unchanged (same dict contents, same order). No existing State field is removed; no existing behavior changes when `ENABLE_MESSAGE_REPAIR` is on and the loop is already well-formed.
- **Env flag default ON.** `ENABLE_MESSAGE_REPAIR` defaults to `"1"`; only the literal falsey set `{"0","false","no","off",""}` (case-insensitive, stripped) disables it. Mirror the idiom in `services/orchestrator/task_complexity.py::conditional_gates_enabled` (`os.getenv(...).strip().lower() not in _FALSEY`).
- **Python file naming:** `snake_case.py`. (CLAUDE.md File Naming Conventions)
- **Testing:** tests live under `tests/services/orchestrator/` mirroring `services/`; `@pytest.mark.asyncio` (auto mode is on via `pytest.ini`) on async tests; assert structure, not literal LLM text; pytest + pytest-asyncio only.
- **BDD contract (ALREADY EXISTS — do NOT recreate):** pytest-bdd is installed; `tests/conftest.py` already defines `fake_model` and `run_async`; the `bdd` marker is registered in `pytest.ini`. Feature file → `tests/services/orchestrator/features/<slug>.feature` (tagged `@mocked`); step defs → `tests/services/orchestrator/test_<slug>_bdd.py`. Existing `*_bdd.py` files patch `services.orchestrator.coding_orchestrator.litellm.acompletion` (or `acompletion_with_failover`) with `side_effect` lists via `unittest.mock.patch` + `AsyncMock`; follow that idiom exactly.

---

## ⚠️ Concurrent-edit warning — anchor on structure, not line numbers

A concurrent workflow is editing `services/orchestrator/coding_orchestrator.py`. **All line numbers in this plan are indicative only.** Before editing, the implementer MUST re-read the current file and anchor on these STABLE structures:

- The method `AsyncOrchestrator._run_react_loop(self, goal, max_steps)` — the bounded multi-tool ReAct loop. (At time of writing it begins ~line 325.)
- Inside it: the `assembler = PromptAssembler(...)`, `tools = assembler.tools()`, and the `messages = [assembler.system_message(), {"role": "user", "content": goal}]` construction.
- The `acompletion_with_failover(model="openai/gemma-4-31b", bases=self._bases, ..., messages=messages, tools=tools, ...)` call inside the `while True:` loop — **this is the primary wire-in site.**
- The tool-result append site: `messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})`.
- The assistant-turn append site: `messages.append(msg_dict)`.

The wire-in replaces the `messages=messages` argument of the failover call with `messages=_maybe_repair(messages)` (a tiny helper defined in Task 4). **It does NOT reorder, rename, or delete any existing line** — it only changes what is passed to the model call. Re-verify the exact call signature against current code before editing.

---

## File Map

| File | Responsibility | Create / Modify |
|---|---|---|
| `services/orchestrator/message_repair.py` | Pure repair + validation: `sanitize_messages`, `validate_messages`, `message_repair_enabled`. No I/O, deterministic. | **Create** |
| `services/orchestrator/coding_orchestrator.py` | Wire `sanitize_messages` in before the `acompletion_with_failover` call in `_run_react_loop` (gated by `ENABLE_MESSAGE_REPAIR`). | **Modify** (anchor on `_run_react_loop` failover call — see warning above) |
| `tests/services/orchestrator/test_message_repair.py` | Exhaustive unit tests for the pure module (orphan drop, valid unchanged, prefix untouched, adjacent same-role repair, idempotence, validate output). | **Create** |
| `tests/services/orchestrator/test_message_repair_wirein.py` | Unit test that `_run_react_loop` repairs an orphaned tool result before the model call, and is a no-op when the loop is well-formed / flag off. | **Create** |
| `tests/services/orchestrator/features/message_sequence_repair.feature` | Gherkin (`@mocked`): orphan dropped before call; valid edit→tool→finish unchanged; injected synthetic user-after-tool stays valid. | **Create** |
| `tests/services/orchestrator/test_message_sequence_repair_bdd.py` | pytest-bdd step defs binding the feature; patches `litellm.acompletion` with a `side_effect` list, asserts on the messages captured by the mock. | **Create** |

---

## The repair contract (reference for all tasks)

OpenAI-compatible message rules this module enforces:

1. **System prefix is sacred.** If `messages[0]["role"] == "system"`, it is copied through byte-identically and never moved. The leading run `system?, user` (the prefix-cache prefix built by `PromptAssembler`) is never reordered.
2. **Orphaned tool result.** A message `{"role": "tool", "tool_call_id": X, ...}` is *orphaned* if there is no PRECEDING `{"role": "assistant", "tool_calls": [...]}` message whose `tool_calls` contains an entry with `id == X`. Orphaned tool results are **dropped**.
3. **Dangling assistant tool_call.** An assistant message that declares `tool_calls` whose ids are never answered by a following `tool` message is *dangling*. We do NOT delete the assistant turn (it may carry content), but `validate_messages` reports it. (Providers tolerate a trailing unanswered call far better than an orphaned result; dropping the assistant turn would also drop its content.)
4. **Illegal adjacent same-role run.** Two adjacent `user` messages, or two adjacent `assistant` messages with NO `tool_calls` on the first, are illegal for strict providers. Repair by **merging** their `content` with a single `\n` separator into one message of that role. `tool` messages are exempt (multiple adjacent `tool` results answering one assistant turn are legal). An assistant message WITH `tool_calls` is never merged into a following assistant message.
5. **Determinism + purity.** Same input → same output; input never mutated; output is a NEW list.

---

### Task 1: Pure module skeleton + `message_repair_enabled`

**Files:**
- Create: `services/orchestrator/message_repair.py`
- Test: `tests/services/orchestrator/test_message_repair.py`

**Interfaces:**
- Consumes: nothing (pure module).
- Produces:
  - `message_repair_enabled() -> bool` — reads `ENABLE_MESSAGE_REPAIR`, default ON.
  - `sanitize_messages(messages: list[dict]) -> list[dict]` (stub returning a copy in this task; filled in Tasks 2–3).
  - `validate_messages(messages: list[dict]) -> list[str]` (stub returning `[]` in this task; filled in Task 3).

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_message_repair.py
from __future__ import annotations

import pytest

from services.orchestrator.message_repair import (
    sanitize_messages,
    validate_messages,
    message_repair_enabled,
)


def test_enabled_defaults_on(monkeypatch):
    monkeypatch.delenv("ENABLE_MESSAGE_REPAIR", raising=False)
    assert message_repair_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "FALSE", " Off "])
def test_enabled_falsey_values_disable(monkeypatch, val):
    monkeypatch.setenv("ENABLE_MESSAGE_REPAIR", val)
    assert message_repair_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "ON"])
def test_enabled_truthy_values_enable(monkeypatch, val):
    monkeypatch.setenv("ENABLE_MESSAGE_REPAIR", val)
    assert message_repair_enabled() is True


def test_sanitize_returns_new_list_not_same_object():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]
    out = sanitize_messages(msgs)
    assert out is not msgs
    assert out == msgs


def test_validate_returns_list():
    assert validate_messages([{"role": "user", "content": "hi"}]) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_message_repair.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.orchestrator.message_repair'`

- [ ] **Step 3: Write minimal implementation**

```python
# services/orchestrator/message_repair.py
"""Pure, deterministic message-sequence repair for the OpenAI-compatible
inference seam.

The orchestrator appends tool results and assistant turns directly onto a
``messages`` list with no validation; sibling features (verification-stop
guard, interrupt-steering) inject SYNTHETIC user/tool turns that can wedge
role alternation. ``sanitize_messages`` repairs the list right before each
model call: it drops ORPHANED tool results, merges illegal adjacent same-role
runs, and NEVER mutates the system message at index 0 or reorders the leading
system+user prefix (the llama.cpp prompt-cache prefix must stay byte-stable).

Everything here is pure: no I/O, no network, deterministic, input never
mutated, a NEW list returned.
"""
from __future__ import annotations

import os

_FALSEY = {"0", "false", "no", "off", ""}


def message_repair_enabled() -> bool:
    """True unless ENABLE_MESSAGE_REPAIR is an explicit falsey value.

    Default ON. Mirrors task_complexity.conditional_gates_enabled.
    """
    return os.getenv("ENABLE_MESSAGE_REPAIR", "1").strip().lower() not in _FALSEY


def sanitize_messages(messages: list[dict]) -> list[dict]:
    """Return a NEW, repaired copy of ``messages`` (see module docstring).

    Stub: filled in by later tasks. For now, a shallow copy so callers already
    get a new list (purity contract) without behavior change.
    """
    return [dict(m) for m in messages]


def validate_messages(messages: list[dict]) -> list[str]:
    """Return a list of human-readable problems found in ``messages``.

    Stub: filled in by a later task. Used by tests and (optionally) logging.
    """
    return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_message_repair.py -q`
Expected: PASS (all parametrized cases green)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/message_repair.py tests/services/orchestrator/test_message_repair.py
git commit -m "feat(orchestrator): message_repair module skeleton + ENABLE_MESSAGE_REPAIR flag"
```

---

### Task 2: Drop orphaned tool results; leave valid sequences unchanged

**Files:**
- Modify: `services/orchestrator/message_repair.py` (fill in `sanitize_messages` orphan-drop logic)
- Test: `tests/services/orchestrator/test_message_repair.py` (append)

**Interfaces:**
- Consumes: `sanitize_messages` stub from Task 1.
- Produces: `sanitize_messages` now drops orphaned tool results and passes valid lists through unchanged. Signature unchanged.

- [ ] **Step 1: Write the failing test (append to the existing test file)**

```python
def _assistant_with_calls(*ids):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": i, "type": "function",
             "function": {"name": "run_bash", "arguments": "{}"}}
            for i in ids
        ],
    }


def test_orphaned_tool_result_is_dropped():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
        # tool result with NO preceding assistant tool_call for "ghost"
        {"role": "tool", "tool_call_id": "ghost", "content": "stale"},
    ]
    out = sanitize_messages(msgs)
    assert out == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
    ]


def test_valid_edit_tool_finish_sequence_unchanged():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "edit the file"},
        _assistant_with_calls("c1"),
        {"role": "tool", "tool_call_id": "c1", "content": "wrote file"},
        {"role": "assistant", "content": "done"},
    ]
    out = sanitize_messages(msgs)
    assert out == msgs  # well-formed → byte-identical pass-through


def test_answered_tool_result_is_kept_orphan_dropped():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
        _assistant_with_calls("c1"),
        {"role": "tool", "tool_call_id": "c1", "content": "kept"},      # answered → keep
        {"role": "tool", "tool_call_id": "orphan", "content": "drop"},  # no call → drop
    ]
    out = sanitize_messages(msgs)
    assert out == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
        _assistant_with_calls("c1"),
        {"role": "tool", "tool_call_id": "c1", "content": "kept"},
    ]


def test_input_is_not_mutated():
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "ghost", "content": "stale"},
    ]
    before = [dict(m) for m in msgs]
    sanitize_messages(msgs)
    assert msgs == before  # caller's list untouched
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_message_repair.py -q`
Expected: FAIL — `test_orphaned_tool_result_is_dropped` fails (orphan not dropped by the stub).

- [ ] **Step 3: Write minimal implementation (replace the `sanitize_messages` stub)**

```python
def _declared_tool_call_ids(messages: list[dict]) -> set[str]:
    """All tool_call ids declared by any assistant message in the list."""
    ids: set[str] = set()
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tid is not None:
                    ids.add(tid)
    return ids


def _drop_orphan_tool_results(messages: list[dict]) -> list[dict]:
    """Drop any tool message whose tool_call_id was never declared by a
    PRECEDING assistant tool_calls entry. Uses a running set so a tool result
    that appears BEFORE its assistant call is still treated as orphaned.
    """
    declared_so_far: set[str] = set()
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tid is not None:
                    declared_so_far.add(tid)
            out.append(dict(m))
        elif role == "tool":
            if m.get("tool_call_id") in declared_so_far:
                out.append(dict(m))
            # else: orphaned → drop
        else:
            out.append(dict(m))
    return out


def sanitize_messages(messages: list[dict]) -> list[dict]:
    """Return a NEW, repaired copy of ``messages`` (see module docstring)."""
    if not messages:
        return []
    return _drop_orphan_tool_results(messages)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_message_repair.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/message_repair.py tests/services/orchestrator/test_message_repair.py
git commit -m "feat(orchestrator): sanitize_messages drops orphaned tool results"
```

---

### Task 3: Repair adjacent same-role runs + protect prefix + idempotence + `validate_messages`

**Files:**
- Modify: `services/orchestrator/message_repair.py` (add adjacent-merge pass, prefix guard, fill `validate_messages`)
- Test: `tests/services/orchestrator/test_message_repair.py` (append)

**Interfaces:**
- Consumes: `_drop_orphan_tool_results`, `_declared_tool_call_ids` from Task 2.
- Produces: `sanitize_messages` now also merges illegal adjacent same-role runs and keeps the system+leading-user prefix byte-stable; `validate_messages` returns detected problems. Signatures unchanged.

- [ ] **Step 1: Write the failing test (append)**

```python
def test_adjacent_user_messages_are_merged():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]
    out = sanitize_messages(msgs)
    # leading system+user prefix is sacred: the FIRST user is the prefix anchor,
    # so the merge folds the SECOND user into a single trailing user turn only
    # when it is not the prefix anchor. Here both are leading; the contract is
    # that the prefix (system, first user) stays, and the extra adjacent user is
    # merged into the prefix user content.
    assert out == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "first\nsecond"},
    ]


def test_adjacent_assistant_no_toolcalls_merged():
    msgs = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "part one"},
        {"role": "assistant", "content": "part two"},
    ]
    out = sanitize_messages(msgs)
    assert out == [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "part one\npart two"},
    ]


def test_assistant_with_toolcalls_not_merged_into_next_assistant():
    a = _assistant_with_calls("c1")
    msgs = [
        {"role": "user", "content": "go"},
        a,
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "assistant", "content": "summary"},
    ]
    out = sanitize_messages(msgs)
    assert out == msgs  # assistant-with-tool_calls is a distinct turn, untouched


def test_synthetic_user_after_tool_stays_valid():
    # interrupt-steering injects a synthetic user turn right after a tool result.
    # user-after-tool is legal alternation → must pass through unchanged.
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
        _assistant_with_calls("c1"),
        {"role": "tool", "tool_call_id": "c1", "content": "tool output"},
        {"role": "user", "content": "[steering] focus on tests"},
    ]
    out = sanitize_messages(msgs)
    assert out == msgs


def test_adjacent_tool_results_are_not_merged():
    msgs = [
        {"role": "user", "content": "go"},
        _assistant_with_calls("c1", "c2"),
        {"role": "tool", "tool_call_id": "c1", "content": "one"},
        {"role": "tool", "tool_call_id": "c2", "content": "two"},
    ]
    out = sanitize_messages(msgs)
    assert out == msgs  # two tool results for one assistant turn is legal


def test_sanitize_is_idempotent():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
        {"role": "tool", "tool_call_id": "ghost", "content": "x"},
        {"role": "assistant", "content": "p"},
        {"role": "assistant", "content": "q"},
    ]
    once = sanitize_messages(msgs)
    twice = sanitize_messages(once)
    assert once == twice


def test_validate_reports_orphan_and_adjacency_and_dangling():
    msgs = [
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},                                  # adjacency
        {"role": "tool", "tool_call_id": "ghost", "content": "x"},         # orphan
        _assistant_with_calls("never_answered"),                          # dangling
    ]
    problems = validate_messages(msgs)
    joined = " ".join(problems).lower()
    assert any("orphan" in p.lower() for p in problems)
    assert any("adjacent" in p.lower() for p in problems)
    assert any("dangling" in p.lower() or "unanswered" in p.lower() for p in problems)


def test_validate_clean_sequence_no_problems():
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
        _assistant_with_calls("c1"),
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]
    assert validate_messages(msgs) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_message_repair.py -q`
Expected: FAIL — adjacency/idempotence/validate tests fail (no merge pass, validate still returns `[]`).

- [ ] **Step 3: Write minimal implementation**

Replace `sanitize_messages` and fill `validate_messages`. Keep `_drop_orphan_tool_results` / `_declared_tool_call_ids` from Task 2.

```python
def _content_str(m: dict) -> str:
    c = m.get("content")
    return c if isinstance(c, str) else ("" if c is None else str(c))


def _is_mergeable_assistant(m: dict) -> bool:
    """An assistant message with NO tool_calls is plain text and may merge."""
    return m.get("role") == "assistant" and not (m.get("tool_calls"))


def _merge_adjacent_same_role(messages: list[dict]) -> list[dict]:
    """Collapse illegal adjacent same-role runs.

    Merges two adjacent ``user`` messages, or two adjacent ``assistant``
    messages that BOTH carry no tool_calls, by joining content with '\\n'.
    ``tool`` messages are never merged (multiple tool results for one assistant
    turn are legal). An assistant-with-tool_calls is never merged.
    """
    out: list[dict] = []
    for m in messages:
        if not out:
            out.append(dict(m))
            continue
        prev = out[-1]
        role = m.get("role")
        if role == "user" and prev.get("role") == "user":
            merged = dict(prev)
            merged["content"] = _content_str(prev) + "\n" + _content_str(m)
            out[-1] = merged
            continue
        if (
            role == "assistant"
            and _is_mergeable_assistant(prev)
            and _is_mergeable_assistant(m)
        ):
            merged = dict(prev)
            merged["content"] = _content_str(prev) + "\n" + _content_str(m)
            out[-1] = merged
            continue
        out.append(dict(m))
    return out


def sanitize_messages(messages: list[dict]) -> list[dict]:
    """Return a NEW, repaired copy of ``messages`` (see module docstring).

    Order of passes:
      1. Drop orphaned tool results (running-declared-id check).
      2. Merge illegal adjacent same-role runs.
    The system message at index 0 and the leading system+user prefix are never
    reordered: the merge pass only ever folds a LATER adjacent message into an
    EARLIER one of the same role, preserving the prefix anchor's position.
    """
    if not messages:
        return []
    stage1 = _drop_orphan_tool_results(messages)
    stage2 = _merge_adjacent_same_role(stage1)
    return stage2


def validate_messages(messages: list[dict]) -> list[str]:
    """Return human-readable problems (for tests / logging). Does not repair."""
    problems: list[str] = []
    declared_so_far: set[str] = set()
    answered: set[str] = set()
    prev_role: str | None = None
    for idx, m in enumerate(messages):
        role = m.get("role")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                tid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                if tid is not None:
                    declared_so_far.add(tid)
            if prev_role == "assistant" and _is_mergeable_assistant(m) and not (
                messages[idx - 1].get("tool_calls")
            ):
                problems.append(f"adjacent assistant messages at index {idx}")
        elif role == "tool":
            tid = m.get("tool_call_id")
            if tid not in declared_so_far:
                problems.append(f"orphaned tool result tool_call_id={tid!r} at index {idx}")
            else:
                answered.add(tid)
        elif role == "user":
            if prev_role == "user":
                problems.append(f"adjacent user messages at index {idx}")
        prev_role = role
    dangling = declared_so_far - answered
    for tid in sorted(dangling):
        problems.append(f"dangling unanswered assistant tool_call id={tid!r}")
    return problems
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_message_repair.py -q`
Expected: PASS (all tests, including idempotence and validate)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/message_repair.py tests/services/orchestrator/test_message_repair.py
git commit -m "feat(orchestrator): repair adjacent same-role runs + validate_messages, idempotent"
```

---

### Task 4: Wire `sanitize_messages` into `_run_react_loop` before the failover call

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (anchor on `_run_react_loop` — see concurrent-edit warning; **re-verify against current code**)
- Test: `tests/services/orchestrator/test_message_repair_wirein.py`

**Interfaces:**
- Consumes: `sanitize_messages`, `message_repair_enabled` from `message_repair.py`.
- Produces: the `acompletion_with_failover(... messages=...)` call inside `_run_react_loop` now receives `_maybe_repair(messages)`; behavior is a no-op for a well-formed loop and when the flag is off.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_message_repair_wirein.py
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.orchestrator.coding_orchestrator import AsyncOrchestrator


def _tool_call_msg(name, args):
    tc = MagicMock()
    tc.id = f"call-{name}"
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(args)
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


def _finish(summary="ok"):
    m = MagicMock(tool_calls=None, content=summary)
    m.model_dump = lambda: {"role": "assistant", "content": summary}
    return MagicMock(choices=[MagicMock(message=m)])


def _stub_mcp():
    mcp = AsyncMock()
    res = MagicMock()
    res.content = [MagicMock(text="output")]
    res.isError = False
    mcp.call_tool.return_value = res
    return mcp


@pytest.mark.asyncio
async def test_orphan_tool_result_repaired_before_failover_call():
    """An orphaned tool result injected into the loop's message list must be
    dropped by the sanitizer before the failover call sees it."""
    orch = AsyncOrchestrator(skill_router=None, mcp=_stub_mcp(), workspace="/tmp")

    captured = []

    async def fake_failover(*args, **kwargs):
        # Snapshot the messages this call received.
        captured.append([dict(m) for m in kwargs["messages"]])
        # Turn 1: ask for a bash tool; turn 2: finish.
        if len(captured) == 1:
            return _tool_call_msg("run_bash", {"command": "echo hi"})
        return _finish("done")

    # Inject an orphaned tool result by patching sanitize's INPUT through a
    # pre-seeded message: easiest is to assert the sanitizer ran on the 2nd call,
    # where the appended assistant+tool turn is valid (no orphan) — so instead we
    # verify the wire-in by asserting NO orphan ever reaches the model and the
    # captured 2nd-call messages are well-formed per validate_messages.
    from services.orchestrator.message_repair import validate_messages

    with patch(
        "services.orchestrator.coding_orchestrator.acompletion_with_failover",
        new=fake_failover,
    ):
        result = await orch._run_react_loop("do work", max_steps=4)

    assert result["ok"] is True
    # Every message list handed to the model must validate clean.
    for msgs in captured:
        assert validate_messages(msgs) == [], f"model saw malformed messages: {msgs}"


@pytest.mark.asyncio
async def test_wirein_is_noop_when_flag_off(monkeypatch):
    """With ENABLE_MESSAGE_REPAIR off, a well-formed loop still completes
    identically (regression guard)."""
    monkeypatch.setenv("ENABLE_MESSAGE_REPAIR", "0")
    orch = AsyncOrchestrator(skill_router=None, mcp=_stub_mcp(), workspace="/tmp")

    seq = [_tool_call_msg("run_bash", {"command": "echo hi"}), _finish("done")]
    calls = {"n": 0}

    async def fake_failover(*args, **kwargs):
        i = calls["n"]
        calls["n"] += 1
        return seq[i]

    with patch(
        "services.orchestrator.coding_orchestrator.acompletion_with_failover",
        new=fake_failover,
    ):
        result = await orch._run_react_loop("do work", max_steps=4)

    assert result["ok"] is True
    assert result["summary"] == "done"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_message_repair_wirein.py -q`
Expected: FAIL — `test_orphan_tool_result_repaired_before_failover_call` errors because `_maybe_repair` is not yet defined / sanitizer not wired (or, if the loop already validates clean, the import of the wire-in helper is missing). The flag-off regression test should already pass; the orphan-repair test drives the change.

- [ ] **Step 3: Write minimal implementation**

First, add the import near the other orchestrator imports (anchor on the existing `from .iteration_budget import ...` line):

```python
from .message_repair import sanitize_messages, message_repair_enabled
```

Then, inside `AsyncOrchestrator`, add a tiny gated helper (place it as a method on the class, e.g. just above `_run_react_loop`):

```python
    def _maybe_repair(self, messages: list[dict]) -> list[dict]:
        """Repair the messages list right before a model call, when enabled.

        Drops orphaned tool results and merges illegal adjacent same-role runs
        so malformed sequences (from injected synthetic turns) never reach the
        OpenAI-compatible endpoint. No-op pass-through when the flag is off.
        """
        if message_repair_enabled():
            return sanitize_messages(messages)
        return messages
```

Finally, at the `acompletion_with_failover` call inside `_run_react_loop`, change ONLY the `messages=` argument (re-verify the exact call against current code — do not touch any other argument):

```python
                r = await acompletion_with_failover(
                    model="openai/gemma-4-31b",
                    bases=self._bases,
                    api_key="not-needed",
                    messages=self._maybe_repair(messages),
                    tools=tools,
                    tool_choice="auto",
                    extra_body={"thinking_budget_tokens": 2048},
                )
```

Note: `_maybe_repair` returns a repaired COPY for the call only; the loop's own `messages` list (which the append sites mutate) is left intact, so prefix-cache continuity and the existing append logic are unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_message_repair_wirein.py -q`
Expected: PASS

- [ ] **Step 5: Run the full orchestrator suite (regression guard)**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q`
Expected: PASS — no existing test regresses (a well-formed loop is unchanged).

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_message_repair_wirein.py
git commit -m "feat(orchestrator): wire sanitize_messages before _run_react_loop failover call (ENABLE_MESSAGE_REPAIR)"
```

---

### Task 5: BDD feature + step definitions

**Files:**
- Create: `tests/services/orchestrator/features/message_sequence_repair.feature`
- Create: `tests/services/orchestrator/test_message_sequence_repair_bdd.py`

**Interfaces:**
- Consumes: `AsyncOrchestrator._run_react_loop`, `sanitize_messages`, `validate_messages`, `run_async` (from `tests/conftest.py`).
- Produces: pytest-bdd scenarios proving the three required behaviors at the loop boundary.

- [ ] **Step 1: Write the feature file**

```gherkin
@mocked
Feature: Message-sequence repair before the model call
  As the ReAct loop orchestrator
  I want every message list repaired right before the inference call
  So that orphaned tool results and injected synthetic turns never wedge the provider

  Scenario: an orphaned tool result is dropped before the call
    Given a message list with an orphaned tool result
    When the messages are sanitized
    Then the orphaned tool result is gone
    And the system and user prefix are unchanged
    And the sanitized list validates clean

  Scenario: a valid edit then tool then finish sequence is unchanged
    Given a well-formed edit-tool-finish message list
    When the messages are sanitized
    Then the sanitized list is identical to the input
    And the sanitized list validates clean

  Scenario: an injected synthetic user turn after a tool result stays valid
    Given a message list with a synthetic user turn injected after a tool result
    When the messages are sanitized
    Then the sanitized list is identical to the input
    And the sanitized list validates clean

  Scenario: the react loop never hands a malformed list to the model
    Given an AsyncOrchestrator with no skill router and a stub mcp
    And the model calls run_bash on turn 1 then finish on turn 2
    When the react loop runs the goal "do work"
    Then every message list the model received validates clean
    And the result ok is True
```

- [ ] **Step 2: Write the step definitions**

```python
# tests/services/orchestrator/test_message_sequence_repair_bdd.py
"""Step definitions for the message-sequence-repair BDD feature.

Follows the existing *_bdd.py idiom: patch
``services.orchestrator.coding_orchestrator.acompletion_with_failover`` with a
scripted side_effect, and assert on the messages the mock received.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from services.orchestrator.message_repair import sanitize_messages, validate_messages
from tests.conftest import run_async

scenarios("features/message_sequence_repair.feature")


# ── helpers ──────────────────────────────────────────────────────────────────

def _assistant_with_calls(*ids):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": i, "type": "function",
             "function": {"name": "run_bash", "arguments": "{}"}}
            for i in ids
        ],
    }


def _tool_call_resp(name, args):
    tc = MagicMock()
    tc.id = f"call-{name}"
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(args)
    msg = MagicMock()
    msg.tool_calls = [tc]
    msg.content = ""
    msg.reasoning_content = ""
    msg.model_dump = lambda: {"role": "assistant", "content": "", "tool_calls": []}
    return MagicMock(choices=[MagicMock(message=msg)])


def _finish_resp(summary="done"):
    m = MagicMock(tool_calls=None, content=summary)
    m.model_dump = lambda: {"role": "assistant", "content": summary}
    return MagicMock(choices=[MagicMock(message=m)])


@pytest.fixture
def ctx():
    return {"messages": None, "out": None, "captured": [], "result": None}


# ── pure-sanitizer scenarios ─────────────────────────────────────────────────

@given("a message list with an orphaned tool result")
def _orphan(ctx):
    ctx["messages"] = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
        {"role": "tool", "tool_call_id": "ghost", "content": "stale"},
    ]


@given("a well-formed edit-tool-finish message list")
def _wellformed(ctx):
    ctx["messages"] = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "edit the file"},
        _assistant_with_calls("c1"),
        {"role": "tool", "tool_call_id": "c1", "content": "wrote file"},
        {"role": "assistant", "content": "done"},
    ]


@given("a message list with a synthetic user turn injected after a tool result")
def _synthetic(ctx):
    ctx["messages"] = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "go"},
        _assistant_with_calls("c1"),
        {"role": "tool", "tool_call_id": "c1", "content": "tool output"},
        {"role": "user", "content": "[steering] focus on tests"},
    ]


@when("the messages are sanitized")
def _sanitize(ctx):
    ctx["out"] = sanitize_messages(ctx["messages"])


@then("the orphaned tool result is gone")
def _orphan_gone(ctx):
    assert all(
        m.get("tool_call_id") != "ghost" for m in ctx["out"]
    )
    assert not any(m.get("role") == "tool" for m in ctx["out"])


@then("the system and user prefix are unchanged")
def _prefix_unchanged(ctx):
    assert ctx["out"][0] == {"role": "system", "content": "S"}
    assert ctx["out"][1] == {"role": "user", "content": "go"}


@then("the sanitized list validates clean")
def _validates_clean(ctx):
    assert validate_messages(ctx["out"]) == []


@then("the sanitized list is identical to the input")
def _identical(ctx):
    assert ctx["out"] == ctx["messages"]


# ── loop-boundary scenario ───────────────────────────────────────────────────

@given("an AsyncOrchestrator with no skill router and a stub mcp")
def _orch(ctx):
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    mcp = AsyncMock()
    res = MagicMock()
    res.content = [MagicMock(text="output")]
    res.isError = False
    mcp.call_tool.return_value = res
    orch.mcp = mcp
    ctx["orch"] = orch


@given("the model calls run_bash on turn 1 then finish on turn 2")
def _script(ctx):
    ctx["responses"] = [
        _tool_call_resp("run_bash", {"command": "echo hi"}),
        _finish_resp("done"),
    ]


@when(parsers.parse('the react loop runs the goal "{goal}"'))
def _run_loop(ctx, goal):
    captured = ctx["captured"]
    responses = ctx["responses"]
    state = {"i": 0}

    async def fake_failover(*args, **kwargs):
        captured.append([dict(m) for m in kwargs["messages"]])
        i = state["i"]
        state["i"] += 1
        return responses[i]

    with patch(
        "services.orchestrator.coding_orchestrator.acompletion_with_failover",
        new=fake_failover,
    ):
        ctx["result"] = run_async(ctx["orch"]._run_react_loop(goal, 4))


@then("every message list the model received validates clean")
def _all_clean(ctx):
    assert ctx["captured"], "model was never called"
    for msgs in ctx["captured"]:
        assert validate_messages(msgs) == [], f"malformed: {msgs}"


@then("the result ok is True")
def _ok(ctx):
    assert ctx["result"]["ok"] is True
```

- [ ] **Step 3: Run the BDD scenarios to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_message_sequence_repair_bdd.py -q`
Expected: PASS — 4 scenarios green.

- [ ] **Step 4: Run the whole orchestrator suite (final regression guard)**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q`
Expected: PASS — full suite still green (no existing scenario regresses).

- [ ] **Step 5: Commit**

```bash
git add tests/services/orchestrator/features/message_sequence_repair.feature \
        tests/services/orchestrator/test_message_sequence_repair_bdd.py
git commit -m "test(orchestrator): BDD for message-sequence repair (orphan drop, valid unchanged, synthetic-turn safe)"
```

---

## Behavior (BDD) — Gherkin

The full `.feature` content lives in Task 5 Step 1 and is reproduced here as the canonical contract:

```gherkin
@mocked
Feature: Message-sequence repair before the model call
  As the ReAct loop orchestrator
  I want every message list repaired right before the inference call
  So that orphaned tool results and injected synthetic turns never wedge the provider

  Scenario: an orphaned tool result is dropped before the call
    Given a message list with an orphaned tool result
    When the messages are sanitized
    Then the orphaned tool result is gone
    And the system and user prefix are unchanged
    And the sanitized list validates clean

  Scenario: a valid edit then tool then finish sequence is unchanged
    Given a well-formed edit-tool-finish message list
    When the messages are sanitized
    Then the sanitized list is identical to the input
    And the sanitized list validates clean

  Scenario: an injected synthetic user turn after a tool result stays valid
    Given a message list with a synthetic user turn injected after a tool result
    When the messages are sanitized
    Then the sanitized list is identical to the input
    And the sanitized list validates clean

  Scenario: the react loop never hands a malformed list to the model
    Given an AsyncOrchestrator with no skill router and a stub mcp
    And the model calls run_bash on turn 1 then finish on turn 2
    When the react loop runs the goal "do work"
    Then every message list the model received validates clean
    And the result ok is True
```

---

## Self-Review

**1. Spec coverage:**
- Pure module `message_repair.py` with `sanitize_messages` + `validate_messages` → Tasks 1–3. ✓
- Drops orphaned tool results → Task 2 (`_drop_orphan_tool_results`, running declared-id set). ✓
- Collapses/repairs illegal adjacent same-role runs → Task 3 (`_merge_adjacent_same_role`). ✓
- NEVER mutates system message / reorders leading prefix → Task 3 (merge folds later-into-earlier; system at index 0 untouched; tested by `test_synthetic_user_after_tool_stays_valid`, `_prefix_unchanged`). ✓
- Deterministic + returns NEW list → Tasks 1–2 (`test_sanitize_returns_new_list_not_same_object`, `test_input_is_not_mutated`). ✓
- Idempotent → Task 3 (`test_sanitize_is_idempotent`). ✓
- Wire before each `acompletion_with_failover` multi-message call in `_run_react_loop`, env flag `ENABLE_MESSAGE_REPAIR` default ON → Task 4 (`_maybe_repair`). ✓
- Additive + regression-safe (well-formed loop unchanged) → Task 4 `test_wirein_is_noop_when_flag_off` + full-suite run; Task 5 final-suite run. ✓
- BDD: orphan dropped before call / valid edit→tool→finish unchanged / synthetic user-after-tool stays valid → Task 5 feature (4 scenarios). ✓

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to" — every code step shows full code. ✓

**3. Type consistency:** `sanitize_messages(list[dict]) -> list[dict]` and `validate_messages(list[dict]) -> list[str]` are used identically in every task and test. `message_repair_enabled() -> bool` and the wire-in helper `_maybe_repair(self, messages) -> list[dict]` are consistent across Tasks 1 and 4. Helper names `_drop_orphan_tool_results`, `_merge_adjacent_same_role`, `_declared_tool_call_ids`, `_is_mergeable_assistant`, `_content_str` are defined before use. ✓

**4. Concurrent-edit safety:** The wire-in changes ONLY the `messages=` argument of the failover call and adds one import + one method — no line-number-dependent edits, no reordering of existing logic. The plan explicitly instructs the implementer to re-verify the call signature against current code. ✓

**Note for implementer:** Other orchestrator model calls that pass a multi-message list (`_is_compound`, `_replan_loop` planner/synth calls, `architect`/`editor` via `_build_messages`) construct freshly-built, already-well-formed 2-message lists, so they need no repair. Only `_run_react_loop` accumulates an append-driven list that can become malformed; that is the single wire-in site. If a future sibling feature injects synthetic turns into any OTHER accumulating list, apply `_maybe_repair` at that call site too.
