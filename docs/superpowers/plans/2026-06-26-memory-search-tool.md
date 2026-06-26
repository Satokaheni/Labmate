# memory_search Tool (Queryable Memory in the ReAct Loop) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a flat `memory_search` tool the model can invoke mid-task inside the ReAct loop to retrieve prior context/decisions from Labmate's vector memory, returned as raw ranked snippets — mirroring the existing `code_semantic_search` tool exactly.

**Architecture:** Today memory is *injection-only*: `ContextManager.build_context()` assembles memory into the system context BEFORE the loop runs, and the frozen `PromptAssembler.tools()` exposes no way to query memory during the loop. This plan adds a first-class `memory_search` tool. A thin, unit-testable `MemorySearch` wrapper wraps an injectable store (anything exposing `async search_memories(query, top_k) -> list[dict]` — `StorageManager` already does) and formats results into raw ranked text. `PromptAssembler` gains a `memory_enabled` flag that inserts the tool schema (gated exactly like `codegraph_enabled` inserts `code_semantic_search`). `AsyncOrchestrator` gains a `memory_search` attribute (set after construction, like `codegraph_mcp`) and a dispatch branch in `_run_react_loop` that calls the wrapper and returns raw text. System-prompt guidance in `BASE_SYSTEM_PROMPT` tells the model to use the tool when it needs prior context.

**Tech Stack:** Python 3.11, asyncio, pytest + pytest-asyncio, pytest-bdd (respx `fake_model` HTTP seam), litellm. No new dependencies.

## Global Constraints

- **stdout is sacred** — never `print()` / `console.log()`; log to stderr via `logging` only.
- **Chroma is client-server mode only** — never `PersistentClient`/`EphemeralClient`. The wrapper reuses `StorageManager.search_memories()` which already uses `AAsyncHttpClient` via `_get_chroma()`; the wrapper itself never touches Chroma directly (it takes an injectable store).
- **async-correct** — no `asyncio.run()` inside async code; the wrapper method is `async def` and is `await`ed in the loop.
- **Additive + regression-safe** — the tool only appears when a memory store is wired, exactly like `code_semantic_search` only appears when `codegraph_mcp` is set. Default-off: `PromptAssembler(memory_enabled=False)` by default; `AsyncOrchestrator.memory_search` defaults to `None`. No existing tool order, signature, or test changes except the additive `memory_enabled` kwarg.
- **Prefix-cache stability** — the new schema is static (no time/uuid/random), inserted at a fixed position, so `canonical_prefix()` stays byte-stable. Position: AFTER `code_semantic_search` (when present), BEFORE the static tail (`read_file…finish`).
- **Tool naming** — Python files `snake_case.py`, classes PascalCase, functions `snake_case`. Tool name is the flat string `memory_search`.
- **Raw, not summarized** — the tool returns the retrieved snippet text verbatim (ranked, truncated for budget), never an LLM summary. Truncate generously: cap each snippet body and cap the total joined output (`_MAX_SNIPPET_CHARS = 600`, `_MAX_TOTAL_CHARS = 4000`) since no shared `tool_grounding` helper exists in this repo (confirmed via grep — only skill-level "grounding" references exist).

---

## File Map

| File | Create/Modify | Responsibility |
|---|---|---|
| `services/orchestrator/memory_search.py` | **Create** | `MemorySearch` wrapper: injectable store, `async search(query, k) -> str` returning raw ranked snippets; pure formatting, unit-testable with a fake store. |
| `services/orchestrator/prompt_assembler.py` | **Modify** | Add `_memory_search_schema()`; add `memory_enabled: bool = False` kwarg to `PromptAssembler.__init__`; insert schema after `code_semantic_search`, before static tail; extend `BASE_SYSTEM_PROMPT` with one guidance sentence. |
| `services/orchestrator/coding_orchestrator.py` | **Modify** | Add `self.memory_search = None` in `AsyncOrchestrator.__init__` (after `self.codegraph_mcp = None`); pass `memory_enabled=self.memory_search is not None` to the `PromptAssembler` built in `_run_react_loop`; add the `memory_search` dispatch branch mirroring `code_semantic_search`. |
| `tests/services/orchestrator/test_memory_search.py` | **Create** | Unit tests for `MemorySearch` with a fake store (no live Chroma/Mongo). |
| `tests/services/orchestrator/test_prompt_assembler.py` | **Modify** | Add tests: tool gated by `memory_enabled`; position; absent by default; prefix stays deterministic. |
| `tests/services/orchestrator/test_memory_search_tool.py` | **Create** | Unit test of the `_run_react_loop` dispatch branch (model calls `memory_search` → wrapper invoked → raw text lands in loop messages; tool absent when no store). |
| `tests/services/orchestrator/features/memory_search.feature` | **Create** | `@mocked` BDD feature (Gherkin below). |
| `tests/services/orchestrator/test_memory_search_bdd.py` | **Create** | pytest-bdd step defs using `fake_model`/scripted responses (mirrors `test_run_tests_tool_bdd.py`). |

**Interfaces produced (relied on by later tasks):**
- `MemorySearch(store, *, max_results: int = 8)` — `store` is any object with `async search_memories(query: str, top_k: int) -> list[dict]`; each dict has keys `id`, `fact` (display text), `raw_fact`, `metadata`, `distance` (the `StorageManager.search_memories` shape).
- `MemorySearch.search(query: str, k: int | None = None) -> str` — async; returns raw ranked snippet text (or a `(no relevant memory found)` sentinel when empty), capped to `_MAX_TOTAL_CHARS`.
- `PromptAssembler(skill_router=None, codegraph_enabled=False, memory_enabled=False, base_system=None, catalog=None)` — `memory_enabled=True` inserts the `memory_search` schema.
- `AsyncOrchestrator.memory_search: MemorySearch | None` — attribute, defaults `None`, set after construction (like `codegraph_mcp`).

---

## Behavior (BDD) — Gherkin

`tests/services/orchestrator/features/memory_search.feature`:

```gherkin
@mocked
Feature: memory_search tool — queryable memory inside the ReAct loop
  As the single-intent ReAct loop
  I want a flat memory_search tool that retrieves prior context from vector memory
  So the model can recall earlier decisions mid-task instead of asking the user to repeat them

  Scenario: memory_search is absent from the tool list when no memory store is wired
    Given an AsyncOrchestrator with no skill router and no memory store
    When the prompt assembler builds the tool list with memory disabled
    Then the tool list does not contain a tool named "memory_search"

  Scenario: memory_search appears in the tool list when a memory store is wired
    Given an AsyncOrchestrator with no skill router and a memory store
    When the prompt assembler builds the tool list with memory enabled
    Then the tool list contains a tool named "memory_search"
    And the memory_search tool has a "query" parameter
    And the memory_search tool has a "k" parameter

  Scenario: the model calls memory_search and ranked snippets land in the loop as raw text
    Given an AsyncOrchestrator with no skill router and a memory store
    And the memory store returns snippets:
      | fact                                        |
      | We chose Postgres over Mongo for billing.   |
      | The retry budget was capped at 2 attempts.  |
    And the model calls memory_search with query "what database for billing" on turn 1
    And the model calls finish with summary "recalled the decision" on turn 2
    When react_execute runs the goal "continue the billing work"
    Then the memory_search tool result contains "Postgres over Mongo"
    And the memory_search tool result contains "retry budget was capped"

  Scenario: memory_search returns the snippets raw, not an LLM summary
    Given an AsyncOrchestrator with no skill router and a memory store
    And the memory store returns snippets:
      | fact                                              |
      | Decision: use AsyncMongoDBSaver, never MemorySaver. |
    And the model calls memory_search with query "checkpointer choice" on turn 1
    And the model calls finish with summary "done" on turn 2
    When react_execute runs the goal "recall the checkpointer decision"
    Then the memory_search tool result contains "AsyncMongoDBSaver, never MemorySaver"

  Scenario: memory_search reports an empty result clearly when memory has nothing
    Given an AsyncOrchestrator with no skill router and a memory store
    And the memory store returns snippets:
      |  |
    And the model calls memory_search with query "nonexistent topic" on turn 1
    And the model calls finish with summary "nothing found" on turn 2
    When react_execute runs the goal "recall an unknown thing"
    Then the memory_search tool result contains "no relevant memory found"
```

> Note the empty-row scenario: a single header-only data table row with an empty cell programs the fake store to return `[]`. The step def treats a blank `fact` cell as "no rows".

---

## Task 1: `MemorySearch` retrieval wrapper

**Files:**
- Create: `services/orchestrator/memory_search.py`
- Test: `tests/services/orchestrator/test_memory_search.py`

**Interfaces:**
- Consumes: a `store` object exposing `async search_memories(query: str, top_k: int) -> list[dict]` (the `StorageManager.search_memories` shape: each dict has `id`, `fact`, `raw_fact`, `metadata`, `distance`).
- Produces: `MemorySearch(store, *, max_results=8)`, `async search(query, k=None) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/orchestrator/test_memory_search.py
from __future__ import annotations

import pytest

from services.orchestrator.memory_search import MemorySearch


class FakeStore:
    """Minimal stand-in for StorageManager.search_memories — no Chroma/Mongo."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, int]] = []

    async def search_memories(self, query: str, top_k: int = 5) -> list[dict]:
        self.calls.append((query, top_k))
        return self.rows[:top_k]


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_search_returns_raw_ranked_snippets():
    store = FakeStore([
        {"id": "1", "fact": "[decision] Use Postgres for billing.", "raw_fact": "Use Postgres for billing.", "metadata": {}, "distance": 0.1},
        {"id": "2", "fact": "[lesson] Retry budget capped at 2.", "raw_fact": "Retry budget capped at 2.", "metadata": {}, "distance": 0.2},
    ])
    ms = MemorySearch(store)
    out = await ms.search("billing database", k=8)
    assert "Use Postgres for billing." in out
    assert "Retry budget capped at 2." in out
    # ranked order preserved (first row first)
    assert out.index("Postgres") < out.index("Retry budget")


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_search_passes_k_through_to_store():
    store = FakeStore([{"id": str(i), "fact": f"fact {i}", "raw_fact": f"fact {i}", "metadata": {}, "distance": 0.0} for i in range(20)])
    ms = MemorySearch(store, max_results=8)
    await ms.search("q", k=3)
    assert store.calls[-1] == ("q", 3)


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_search_defaults_k_to_max_results():
    store = FakeStore([])
    ms = MemorySearch(store, max_results=5)
    await ms.search("q")
    assert store.calls[-1] == ("q", 5)


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_search_clamps_k_to_twenty():
    store = FakeStore([])
    ms = MemorySearch(store)
    await ms.search("q", k=999)
    assert store.calls[-1][1] == 20


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_empty_results_return_sentinel():
    ms = MemorySearch(FakeStore([]))
    out = await ms.search("nothing here")
    assert out == "(no relevant memory found)"


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_blank_facts_are_filtered_then_sentinel():
    ms = MemorySearch(FakeStore([{"id": "1", "fact": "  ", "raw_fact": "", "metadata": {}, "distance": 0.0}]))
    out = await ms.search("q")
    assert out == "(no relevant memory found)"


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_total_output_capped():
    big = "x" * 5000
    store = FakeStore([{"id": str(i), "fact": big, "raw_fact": big, "metadata": {}, "distance": 0.0} for i in range(5)])
    ms = MemorySearch(store)
    out = await ms.search("q")
    assert len(out) <= 4000


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_per_snippet_capped():
    big = "y" * 5000
    store = FakeStore([{"id": "1", "fact": big, "raw_fact": big, "metadata": {}, "distance": 0.0}])
    ms = MemorySearch(store)
    out = await ms.search("q")
    # a single snippet body is capped at 600 chars (+ small index prefix)
    assert len(out) <= 700


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_store_error_returns_error_text_not_raise():
    class Boom:
        async def search_memories(self, query, top_k=5):
            raise RuntimeError("chroma down")

    ms = MemorySearch(Boom())
    out = await ms.search("q")
    assert "memory search failed" in out
    assert "chroma down" in out


@pytest.mark.asyncio
@pytest.mark.mocked
async def test_none_store_returns_unavailable():
    ms = MemorySearch(None)
    out = await ms.search("q")
    assert out == "(memory store not available)"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_memory_search.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.memory_search'`

- [ ] **Step 3: Write the implementation**

```python
# services/orchestrator/memory_search.py
"""Thin, unit-testable wrapper turning Labmate's vector memory into a flat,
ReAct-loop-callable tool.

Mirrors how ``code_semantic_search`` wraps the codegraph MCP: the orchestrator
holds an optional ``MemorySearch`` instance and the loop dispatches to it when
the model calls the ``memory_search`` tool. The wrapper takes an INJECTABLE
store (anything exposing ``async search_memories(query, top_k) -> list[dict]``,
which ``StorageManager`` already does) so it is testable with a fake store and
never touches live Chroma/Mongo itself.

Output is RAW: the retrieved snippet text is returned verbatim, ranked, and
truncated for budget — never summarized by an LLM.
"""
from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger("orchestrator")

# Generous caps (no shared tool_grounding helper exists in this repo).
_MAX_SNIPPET_CHARS = 600
_MAX_TOTAL_CHARS = 4000
_MAX_K = 20  # matches the code_semantic_search "max 20 results" contract


class MemorySearch:
    """Retrieve prior context from vector memory and format it as raw text.

    store: any object with ``async search_memories(query: str, top_k: int)
           -> list[dict]``. Each dict is expected to carry a human-readable
           ``fact`` (preferred) or ``raw_fact`` field. ``None`` is allowed and
           yields a clear "not available" sentinel (regression-safe).
    """

    def __init__(self, store: Any, *, max_results: int = 8) -> None:
        self.store = store
        self.max_results = max_results

    async def search(self, query: str, k: int | None = None) -> str:
        if self.store is None:
            return "(memory store not available)"

        top_k = self.max_results if k is None else int(k)
        top_k = max(1, min(_MAX_K, top_k))

        try:
            rows = await self.store.search_memories(query or "", top_k)
        except Exception as exc:  # never raise into the loop
            _logger.warning("memory_search failed: %s", exc)
            return f"memory search failed: {exc}"

        snippets: list[str] = []
        for i, row in enumerate(rows or [], start=1):
            text = ""
            if isinstance(row, dict):
                text = (row.get("fact") or row.get("raw_fact") or "").strip()
            elif isinstance(row, str):
                text = row.strip()
            if not text:
                continue
            snippets.append(f"[{i}] {text[:_MAX_SNIPPET_CHARS]}")

        if not snippets:
            return "(no relevant memory found)"

        return "\n".join(snippets)[:_MAX_TOTAL_CHARS]


__all__ = ["MemorySearch"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_memory_search.py -q`
Expected: PASS (10 passed)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/memory_search.py tests/services/orchestrator/test_memory_search.py
git commit -m "feat(orchestrator): MemorySearch wrapper for queryable memory

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `memory_search` tool schema + prompt guidance in `PromptAssembler`

**Files:**
- Modify: `services/orchestrator/prompt_assembler.py`
- Test: `tests/services/orchestrator/test_prompt_assembler.py`

**Interfaces:**
- Consumes: nothing from Task 1 at import time (the schema is independent of the wrapper).
- Produces: `PromptAssembler(..., memory_enabled: bool = False)`; `memory_enabled=True` inserts a `memory_search` tool whose params are `{query: str (required), k: int (default 8)}`, positioned after `code_semantic_search` (when present) and before the static tail.

- [ ] **Step 1: Write the failing tests**

Append to `tests/services/orchestrator/test_prompt_assembler.py`:

```python
@pytest.mark.mocked
def test_memory_disabled_by_default_no_memory_search_tool():
    a = PromptAssembler(skill_router=None)
    names = [t["function"]["name"] for t in a.tools()]
    assert "memory_search" not in names


@pytest.mark.mocked
def test_memory_enabled_inserts_memory_search_before_static_tail():
    a = PromptAssembler(skill_router=None, codegraph_enabled=False, memory_enabled=True)
    names = [t["function"]["name"] for t in a.tools()]
    assert names == [
        "memory_search",
        "read_file", "write_file", "list_dir", "run_bash", "run_tests", "finish",
    ]


@pytest.mark.mocked
def test_memory_search_after_code_semantic_search_when_both_enabled():
    a = PromptAssembler(skill_router=None, codegraph_enabled=True, memory_enabled=True)
    names = [t["function"]["name"] for t in a.tools()]
    assert names == [
        "code_semantic_search", "memory_search",
        "read_file", "write_file", "list_dir", "run_bash", "run_tests", "finish",
    ]


@pytest.mark.mocked
def test_memory_search_schema_params():
    a = PromptAssembler(skill_router=None, memory_enabled=True)
    schema = next(t for t in a.tools() if t["function"]["name"] == "memory_search")
    props = schema["function"]["parameters"]["properties"]
    assert "query" in props
    assert "k" in props
    assert schema["function"]["parameters"]["required"] == ["query"]


@pytest.mark.mocked
def test_base_system_prompt_mentions_memory_search():
    a = PromptAssembler(skill_router=None, memory_enabled=True)
    assert "memory_search" in a.system_message()["content"]


@pytest.mark.mocked
def test_memory_enabled_prefix_is_deterministic_and_clean():
    import re
    a = PromptAssembler(skill_router=None, codegraph_enabled=True, memory_enabled=True)
    b = PromptAssembler(skill_router=None, codegraph_enabled=True, memory_enabled=True)
    assert a.prefix_fingerprint() == b.prefix_fingerprint()
    prefix = a.canonical_prefix()
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", prefix)
    assert not re.search(r"[0-9a-f]{32}", prefix)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_prompt_assembler.py -q -k "memory"`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'memory_enabled'`

- [ ] **Step 3: Add the schema builder**

In `services/orchestrator/prompt_assembler.py`, after `_code_semantic_search_schema()` (ends at the line `}` closing that function, before `def _static_tail_schemas()`), insert:

```python
def _memory_search_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "memory_search",
            "description": (
                "Search the agent's long-term memory (past decisions, facts, and "
                "lessons from earlier in this and prior sessions). Returns the top-k "
                "most relevant memory snippets as RAW text. Use this when you suspect "
                "relevant prior context or a past decision exists — search memory "
                "before asking the user to repeat themselves."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What prior context or decision to recall"},
                    "k": {"type": "integer", "description": "Number of snippets (max 20)", "default": 8},
                },
                "required": ["query"],
            },
        },
    }
```

- [ ] **Step 4: Extend `BASE_SYSTEM_PROMPT`**

In `services/orchestrator/prompt_assembler.py`, modify the `BASE_SYSTEM_PROMPT` string. Replace its final line:

```python
    "the code-sandbox skill (load_skill('code-sandbox') then call_skill_tool), NEVER run_bash."
)
```

with:

```python
    "the code-sandbox skill (load_skill('code-sandbox') then call_skill_tool), NEVER run_bash. "
    "MEMORY RULE: when you suspect relevant prior context or a past decision exists, call "
    "memory_search(query) to recall it before asking the user to repeat themselves "
    "(only available when a memory store is wired)."
)
```

- [ ] **Step 5: Add the `memory_enabled` kwarg and insert the schema**

In `PromptAssembler.__init__`, change the signature:

```python
    def __init__(
        self,
        skill_router: Any = None,
        codegraph_enabled: bool = False,
        base_system: str | None = None,
        catalog: str | None = None,
    ) -> None:
```

to add `memory_enabled`:

```python
    def __init__(
        self,
        skill_router: Any = None,
        codegraph_enabled: bool = False,
        memory_enabled: bool = False,
        base_system: str | None = None,
        catalog: str | None = None,
    ) -> None:
```

Then in the same method, locate the tool-assembly block:

```python
        if codegraph_enabled:
            tools.append(_code_semantic_search_schema())
        tools.extend(_static_tail_schemas())
```

and insert the memory schema between the codegraph append and the static tail:

```python
        if codegraph_enabled:
            tools.append(_code_semantic_search_schema())
        if memory_enabled:
            tools.append(_memory_search_schema())
        tools.extend(_static_tail_schemas())
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_prompt_assembler.py -q`
Expected: PASS (all prior assembler tests + the 6 new ones)

- [ ] **Step 7: Commit**

```bash
git add services/orchestrator/prompt_assembler.py tests/services/orchestrator/test_prompt_assembler.py
git commit -m "feat(orchestrator): memory_search tool schema + prompt guidance

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Wire `memory_search` into `_run_react_loop` dispatch

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py`
- Test: `tests/services/orchestrator/test_memory_search_tool.py`

**Interfaces:**
- Consumes: `MemorySearch.search(query, k)` (Task 1); `PromptAssembler(memory_enabled=...)` (Task 2).
- Produces: `AsyncOrchestrator.memory_search: MemorySearch | None` (default `None`); a `memory_search` branch in `_run_react_loop` that calls `await self.memory_search.search(query, k)` and appends the raw string as the tool result.

> **ANCHOR ON STRUCTURE, NOT LINE NUMBERS** — a concurrent workflow edits this file. Do NOT trust the line numbers in the snippets below; re-locate each anchor by its surrounding code before editing:
> 1. The line `self.codegraph_mcp = None  # set after construction ...` inside `AsyncOrchestrator.__init__` — add the `memory_search` attribute right after it.
> 2. In `_run_react_loop`, the `PromptAssembler(...)` construction with `codegraph_enabled=self.codegraph_mcp is not None` — add the `memory_enabled=` kwarg there.
> 3. In `_run_react_loop`, the `elif name == "code_semantic_search":` tool-dispatch branch — add the `memory_search` branch immediately after its closing `else:` block, before the final `else: content = json.dumps({"error": f"unknown tool: {name}"})`.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_memory_search_tool.py
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from services.orchestrator.memory_search import MemorySearch
from tests.conftest import run_async


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


class FakeStore:
    def __init__(self, rows):
        self.rows = rows

    async def search_memories(self, query, top_k=5):
        return self.rows[:top_k]


def _run(orch, goal, responses, captured):
    async def _emit(event_type, **kw):
        if event_type == "tool.done" and "result" in kw:
            captured.append(kw["result"])

    with patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
               new_callable=AsyncMock, side_effect=responses), \
         patch("services.orchestrator.coding_orchestrator.events.emit", new=_emit):
        return run_async(orch.react_execute(goal))


@pytest.mark.mocked
def test_memory_search_branch_returns_raw_snippets_into_loop():
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    orch.memory_search = MemorySearch(FakeStore([
        {"id": "1", "fact": "We chose Postgres over Mongo for billing.", "raw_fact": "", "metadata": {}, "distance": 0.1},
    ]))
    captured: list[str] = []
    responses = [
        _tool_call_msg("memory_search", {"query": "billing database", "k": 5}),
        _tool_call_msg("finish", {"summary": "recalled"}),
    ]
    result = _run(orch, "continue billing", responses, captured)
    assert result["ok"] is True
    assert any("Postgres over Mongo" in c for c in captured)


@pytest.mark.mocked
def test_memory_search_tool_absent_when_no_store():
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    assert orch.memory_search is None
    # Tool not advertised: a model that nonetheless names it gets a clear error,
    # never a crash.
    captured: list[str] = []
    responses = [
        _tool_call_msg("memory_search", {"query": "x"}),
        _tool_call_msg("finish", {"summary": "done"}),
    ]
    _run(orch, "recall something", responses, captured)
    assert any("memory search not available" in c for c in captured)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_memory_search_tool.py -q`
Expected: FAIL — `memory_search` branch falls into `unknown tool: memory_search` (and `orch.memory_search` attribute does not exist yet → AttributeError on first test).

- [ ] **Step 3: Add the attribute and import**

In `services/orchestrator/coding_orchestrator.py`, add the import near the existing `from .prompt_assembler import PromptAssembler`:

```python
from .memory_search import MemorySearch
```

In `AsyncOrchestrator.__init__`, immediately after the line:

```python
        self.codegraph_mcp = None  # set after construction if codegraph-embedder is running
```

add:

```python
        self.memory_search: MemorySearch | None = None  # set after construction when a memory store is wired
```

- [ ] **Step 4: Pass `memory_enabled` to the assembler**

In `_run_react_loop`, change the `PromptAssembler` construction:

```python
        assembler = PromptAssembler(
            skill_router=self.skill_router,
            codegraph_enabled=self.codegraph_mcp is not None,
        )
```

to:

```python
        assembler = PromptAssembler(
            skill_router=self.skill_router,
            codegraph_enabled=self.codegraph_mcp is not None,
            memory_enabled=self.memory_search is not None,
        )
```

- [ ] **Step 5: Add the dispatch branch (mirrors `code_semantic_search`)**

In `_run_react_loop`, locate the `code_semantic_search` branch:

```python
                    elif name == "code_semantic_search":
                        if self.codegraph_mcp is not None:
                            try:
                                obs = await self.codegraph_mcp.call_tool(
                                    "code_semantic_search",
                                    {"query": args.get("query", ""), "k": args.get("k", 8)},
                                )
                                content = "\n".join(
                                    c.text for c in obs.content if hasattr(c, "text")
                                )
                            except Exception as exc:
                                content = json.dumps({"error": str(exc)})
                        else:
                            content = json.dumps({"error": "codegraph semantic search not available"})
```

and add, immediately after it (before the final `else: ... unknown tool`):

```python
                    elif name == "memory_search":
                        if self.memory_search is not None:
                            try:
                                content = await self.memory_search.search(
                                    args.get("query", ""), args.get("k"),
                                )
                            except Exception as exc:
                                content = json.dumps({"error": str(exc)})
                        else:
                            content = json.dumps({"error": "memory search not available"})
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_memory_search_tool.py -q`
Expected: PASS (2 passed)

- [ ] **Step 7: Commit**

```bash
git add services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_memory_search_tool.py
git commit -m "feat(orchestrator): wire memory_search tool into the ReAct loop

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: BDD coverage with `fake_model` / scripted responses

**Files:**
- Create: `tests/services/orchestrator/features/memory_search.feature` (Gherkin from "Behavior (BDD)" above)
- Create: `tests/services/orchestrator/test_memory_search_bdd.py`
- Test: the BDD scenarios themselves

**Interfaces:**
- Consumes: `AsyncOrchestrator`, `MemorySearch`, `PromptAssembler` (Tasks 1–3); `run_async` from `tests/conftest.py`. Mirrors `test_run_tests_tool_bdd.py` exactly: scripted `acompletion_with_failover` side_effect + a `tool.done` capture for the tool-result text. (The loop calls `acompletion_with_failover`, not `litellm.acompletion`, so patch that name — re-verify in the live file.)

- [ ] **Step 1: Create the feature file**

Write `tests/services/orchestrator/features/memory_search.feature` with the exact Gherkin from the "Behavior (BDD) — Gherkin" section above.

- [ ] **Step 2: Write the step defs**

```python
# tests/services/orchestrator/test_memory_search_bdd.py
"""Step definitions for the memory_search tool BDD feature."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.orchestrator.coding_orchestrator import AsyncOrchestrator
from services.orchestrator.memory_search import MemorySearch
from services.orchestrator.prompt_assembler import PromptAssembler
from tests.conftest import run_async

scenarios("features/memory_search.feature")


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


class _FakeStore:
    def __init__(self, rows):
        self.rows = rows

    async def search_memories(self, query, top_k=5):
        return self.rows[:top_k]


@pytest.fixture
def ctx():
    return {"responses": [], "result": None, "tool_results": [], "assembler": None}


def _ensure_len(ctx, turn):
    while len(ctx["responses"]) < turn:
        ctx["responses"].append(_tool_call_msg("finish", {"summary": "filler"}))


# ── Given: orchestrator construction ─────────────────────────────────────────

@given("an AsyncOrchestrator with no skill router and no memory store")
def _orch_no_store(ctx):
    ctx["orch"] = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")


@given("an AsyncOrchestrator with no skill router and a memory store")
def _orch_with_store(ctx):
    orch = AsyncOrchestrator(skill_router=None, mcp=None, workspace="/tmp")
    orch.memory_search = MemorySearch(_FakeStore([]))  # rows set by the table step
    ctx["orch"] = orch


@given(parsers.parse("the memory store returns snippets:\n{table}"))
def _store_rows(ctx, table):
    # Parse the pytest-bdd data table: first line is the header, rest are rows.
    lines = [ln.strip() for ln in table.strip().splitlines()]
    rows = []
    for ln in lines[1:]:
        cell = ln.strip("|").strip()
        if cell:
            rows.append({"id": str(len(rows) + 1), "fact": cell, "raw_fact": cell, "metadata": {}, "distance": 0.0})
    ctx["orch"].memory_search = MemorySearch(_FakeStore(rows))


# ── Given: scripted model turns ──────────────────────────────────────────────

@given(parsers.parse('the model calls memory_search with query "{query}" on turn {turn:d}'))
def _memory_search_turn(ctx, query, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("memory_search", {"query": query})


@given(parsers.parse('the model calls finish with summary "{summary}" on turn {turn:d}'))
def _finish_turn(ctx, summary, turn):
    _ensure_len(ctx, turn)
    ctx["responses"][turn - 1] = _tool_call_msg("finish", {"summary": summary})


# ── When: tool-list build ────────────────────────────────────────────────────

@when("the prompt assembler builds the tool list with memory disabled")
def _build_disabled(ctx):
    ctx["assembler"] = PromptAssembler(skill_router=None, memory_enabled=False)


@when("the prompt assembler builds the tool list with memory enabled")
def _build_enabled(ctx):
    ctx["assembler"] = PromptAssembler(skill_router=None, memory_enabled=True)


# ── When: run the loop ───────────────────────────────────────────────────────

@when(parsers.parse('react_execute runs the goal "{goal}"'))
def _run_goal(ctx, goal):
    captured: list[str] = []

    async def _emit(event_type, **kw):
        if event_type == "tool.done" and "result" in kw:
            captured.append(kw["result"])

    with patch("services.orchestrator.coding_orchestrator.acompletion_with_failover",
               new_callable=AsyncMock, side_effect=ctx["responses"]), \
         patch("services.orchestrator.coding_orchestrator.events.emit", new=_emit):
        ctx["result"] = run_async(ctx["orch"].react_execute(goal))
    ctx["tool_results"] = captured


# ── Then: tool-list assertions ───────────────────────────────────────────────

@then(parsers.parse('the tool list contains a tool named "{name}"'))
def _tool_has(ctx, name):
    names = [t["function"]["name"] for t in ctx["assembler"].tools()]
    assert name in names


@then(parsers.parse('the tool list does not contain a tool named "{name}"'))
def _tool_absent(ctx, name):
    names = [t["function"]["name"] for t in ctx["assembler"].tools()]
    assert name not in names


@then(parsers.parse('the memory_search tool has a "{param}" parameter'))
def _tool_param(ctx, param):
    schema = next(t for t in ctx["assembler"].tools() if t["function"]["name"] == "memory_search")
    assert param in schema["function"]["parameters"]["properties"]


# ── Then: loop result assertions ─────────────────────────────────────────────

@then(parsers.parse('the memory_search tool result contains "{needle}"'))
def _result_contains(ctx, needle):
    joined = "\n".join(ctx["tool_results"])
    assert needle in joined, f"{needle!r} not in {joined!r}"
```

- [ ] **Step 3: Run the BDD scenarios to verify they pass**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/test_memory_search_bdd.py -q`
Expected: PASS (5 scenarios). If pytest-bdd's multi-line data-table `parsers.parse("...:\n{table}")` capture does not bind cleanly in this version, fall back to the simpler `@given` form already proven in the repo: replace the table step with one `@given(parsers.parse('the memory store returns the snippet "{fact}"'))` per row and adjust the feature to one snippet per step. Re-verify which form pytest-bdd accepts before committing.

- [ ] **Step 4: Commit**

```bash
git add tests/services/orchestrator/features/memory_search.feature tests/services/orchestrator/test_memory_search_bdd.py
git commit -m "test(orchestrator): BDD coverage for memory_search tool

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Full regression sweep

**Files:** none (verification only).

- [ ] **Step 1: Run the orchestrator suite**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q`
Expected: PASS — all prior tests green plus the new `memory_search` unit, tool, assembler, and BDD tests. No regressions in `test_prompt_assembler.py` tool-order assertions (the `memory_search` tool is absent unless `memory_enabled=True`, so existing order tests are unaffected).

- [ ] **Step 2: Run the memory suite (confirm no store breakage)**

Run: `cd /Users/zachstallbohm/Work/Labmate && PYTHONPATH=. python -m pytest tests/services/memory/ -q`
Expected: PASS — this plan does not modify `context_manager.py` or `storage_manager.py`, so the memory suite is unchanged.

- [ ] **Step 3: Commit (only if any incidental fixups were needed)**

```bash
git add -A
git commit -m "test: regression sweep for memory_search tool

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage:**
- *Flat `memory_search` tool in `PromptAssembler.tools()` + dispatch branch mirroring `code_semantic_search`* → Tasks 2 (schema, gated by `memory_enabled`) + 3 (dispatch branch).
- *Args `{query: str, k?: int}`* → Task 2 schema (`query` required, `k` default 8); Task 1 wrapper clamps `k` to `[1, 20]`.
- *Calls Labmate's existing memory retrieval (Chroma vector search via Storage/Context) scoped to session* → Task 1 wrapper wraps `store.search_memories(query, top_k)`, which is `StorageManager.search_memories` (Chroma `semantic` collection, valid-only). The injectable store is how scoping/store choice is supplied at wiring time; the wrapper stays store-agnostic and testable.
- *Returns ranked snippets as RAW text, reuse grounding/truncation if a helper exists else cap generously* → Task 1: no `tool_grounding` helper exists (grep-confirmed), so caps `_MAX_SNIPPET_CHARS=600` / `_MAX_TOTAL_CHARS=4000`; raw `fact` text, no LLM summary.
- *System-prompt guidance in `BASE_SYSTEM_PROMPT`* → Task 2 Step 4 ("MEMORY RULE: … search memory before asking the user to repeat themselves").
- *Thin unit-testable wrapper with injectable store, fake-store tested* → Task 1 (`FakeStore`, 10 unit tests, no live Chroma/Mongo).
- *Additive + regression-safe, gated like code_semantic_search (only appears when a store is wired)* → `memory_enabled` defaults `False`; `AsyncOrchestrator.memory_search` defaults `None`; loop passes `memory_enabled=self.memory_search is not None`. Existing tool-order tests untouched.
- *BDD with fake_model shows model calling memory_search and results landing in the loop messages; absent when no store; raw not summarized* → Task 4 (5 scenarios).
- *CLAUDE.md: Chroma client-server, async-correct* → wrapper never touches Chroma directly; `search` is `async` and `await`ed; no `asyncio.run`.

**2. Placeholder scan:** No "TBD"/"add error handling"/"similar to Task N" — every code step shows full code. The one conditional is Task 4 Step 3's documented pytest-bdd data-table fallback, which gives an explicit concrete alternative, not a placeholder.

**3. Type consistency:** `MemorySearch(store, *, max_results=8)` and `async search(query, k=None) -> str` are identical across Tasks 1, 3, 4. `PromptAssembler(..., memory_enabled=False)` identical across Tasks 2, 3, 4. `AsyncOrchestrator.memory_search` attribute (not method) used consistently. Tool name `memory_search` and params `query`/`k` consistent in schema (Task 2), dispatch (Task 3), tests (Tasks 1, 3, 4). The loop patches `acompletion_with_failover` (the name `_run_react_loop` actually calls), flagged for re-verification against the concurrently-edited file.
