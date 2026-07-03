# Local-Harness Piece 0 — Seams + `LABMATE_LOCAL_MODE` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce the `LABMATE_LOCAL_MODE` config flag and a documented seam map, so subsequent migration pieces (checkpointer, sessions, Chroma, Redis, tools, gateway) can slot local implementations in behind the interfaces the brain already uses — with zero behavior change and the full 1413-test orchestrator suite staying green.

**Architecture:** This is Piece 0 of the strangler migration described in `docs/superpowers/specs/2026-07-03-local-harness-rearchitecture-design.md`. It adds a single call-time flag reader (`local_mode.py`) following the repo's existing feature-flag idiom (`message_repair_enabled()`, `conditional_gates_enabled()`), logs the resolved mode at orchestrator startup (proving the flag is wired end-to-end), and lands a seam-map doc that pins the exact file:line insertion point for each later piece. **No behavior branch is added in Piece 0** — the flag is read and logged only. Each later piece adds its own `if local_mode_enabled():` branch at its own insertion point.

**Tech Stack:** Python 3.12, asyncio, pytest + pytest-asyncio. No new dependencies.

## Global Constraints

- Full orchestrator suite `PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q` must stay green (baseline **1413 passed**) after every task.
- Default is **pod mode** (flag OFF) — Piece 0 is strictly behavior-preserving.
- Flag name is exactly `LABMATE_LOCAL_MODE` (verbatim from the spec).
- Flag is read at **call time** via `os.getenv` (never cached at import) so tests can `monkeypatch.setenv` per-test — mirrors `message_repair_enabled()`.
- Repo conventions: never `git add -A` (stage exact paths); never commit `services/frontend/src/config.ts` or `.codegraph/daemon.pid`; commits end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- MCP-server stdout stays sacred; Discord connector stays deferred (not touched here).
- Python files `snake_case`; functions `snake_case`; classes PascalCase.

---

## File Structure

- `services/orchestrator/local_mode.py` (**new**) — the one home for the flag reader. One responsibility: answer "are we in local mode?". Kept as its own module (not folded into `main.py`) so every later piece (`graph.py`, `storage_manager.py`, `coding_orchestrator.py`, `events.py`) can `from .local_mode import local_mode_enabled` without importing the heavy `main.py`.
- `services/orchestrator/main.py` (**modify**) — one added log line in `run()` reading the flag at startup. Proves the seam is reachable from the real entry point.
- `tests/services/orchestrator/test_local_mode.py` (**new**) — unit tests for the flag reader.
- `docs/superpowers/specs/2026-07-03-local-harness-seam-map.md` (**new**) — the strangler insertion-point map: for each subsystem, the exact `file:line` where the flag branch goes and which piece owns it. Analysis deliverable; full content is in Task 3 below.

---

### Task 1: `local_mode_enabled()` flag reader

**Files:**
- Create: `services/orchestrator/local_mode.py`
- Test: `tests/services/orchestrator/test_local_mode.py`

**Interfaces:**
- Consumes: nothing (leaf module; `os` only).
- Produces: `local_mode_enabled() -> bool` — reads `LABMATE_LOCAL_MODE` from the environment at call time. Default OFF. Returns `True` for any non-falsey value; falsey set is `{"0", "false", "no", "off", ""}` (case-insensitive, stripped). Later pieces import this exact name.

- [ ] **Step 1: Write the failing tests**

Create `tests/services/orchestrator/test_local_mode.py`:

```python
"""Unit tests for the LABMATE_LOCAL_MODE flag reader (Piece 0 seam)."""
from __future__ import annotations

import pytest

from services.orchestrator.local_mode import local_mode_enabled


def test_default_is_off(monkeypatch):
    """Unset env -> local mode OFF (pod mode is the default)."""
    monkeypatch.delenv("LABMATE_LOCAL_MODE", raising=False)
    assert local_mode_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "  1  ", "Local"])
def test_truthy_values_enable(monkeypatch, value):
    monkeypatch.setenv("LABMATE_LOCAL_MODE", value)
    assert local_mode_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", "", "  ", "  off  "])
def test_falsey_values_disable(monkeypatch, value):
    monkeypatch.setenv("LABMATE_LOCAL_MODE", value)
    assert local_mode_enabled() is False


def test_read_at_call_time_not_import(monkeypatch):
    """Flipping the env between calls is observed immediately (no import-time cache)."""
    monkeypatch.setenv("LABMATE_LOCAL_MODE", "0")
    assert local_mode_enabled() is False
    monkeypatch.setenv("LABMATE_LOCAL_MODE", "1")
    assert local_mode_enabled() is True
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_local_mode.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.orchestrator.local_mode'`.

- [ ] **Step 3: Write the minimal implementation**

Create `services/orchestrator/local_mode.py`:

```python
"""Single source of truth for the ``LABMATE_LOCAL_MODE`` strangler flag.

Piece 0 of the local-harness re-architecture
(docs/superpowers/specs/2026-07-03-local-harness-rearchitecture-design.md).

When ON, Labmate runs as a self-contained LOCAL harness (SQLite state,
in-process events, direct local tools) instead of the pod-hosted service
(networked Redis/Mongo/Chroma + tool-delegation). Piece 0 only READS and
logs this flag; each later migration piece adds its own ``if
local_mode_enabled():`` branch at its own insertion point (see
docs/superpowers/specs/2026-07-03-local-harness-seam-map.md).

Default OFF (pod mode). Read at call time so tests can flip it per-test;
mirrors ``message_repair_enabled()`` / ``conditional_gates_enabled()``.
"""
from __future__ import annotations

import os

_FALSEY = {"0", "false", "no", "off", ""}


def local_mode_enabled() -> bool:
    """True when ``LABMATE_LOCAL_MODE`` is set to any non-falsey value.

    Default OFF. Falsey set (case-insensitive, whitespace-stripped):
    ``{"0", "false", "no", "off", ""}``.
    """
    return os.getenv("LABMATE_LOCAL_MODE", "0").strip().lower() not in _FALSEY
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_local_mode.py -q`
Expected: PASS (all parametrized cases green).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/local_mode.py tests/services/orchestrator/test_local_mode.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): add LABMATE_LOCAL_MODE flag reader (local-harness Piece 0)

Single call-time flag reader for the strangler migration. Default OFF
(pod mode); mirrors the message_repair_enabled/conditional_gates_enabled
idiom. Behavior-preserving — nothing branches on it yet.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Log the resolved mode at orchestrator startup

**Files:**
- Modify: `services/orchestrator/main.py` (in `OrchestratorProcess.run()`, right after `async with StorageManager() as _sm:` logs "storage ready" at line ~244)
- Test: `tests/services/orchestrator/test_local_mode.py` (add a wiring test)

**Interfaces:**
- Consumes: `local_mode_enabled()` from Task 1.
- Produces: nothing new; a startup log line `orchestrator mode: local` / `orchestrator mode: pod`. This is the end-to-end proof that the flag reaches the real entry point. No control-flow branch.

- [ ] **Step 1: Write the failing wiring test**

Append to `tests/services/orchestrator/test_local_mode.py`:

```python
def test_main_imports_flag_reader():
    """main.py wires the flag reader (guards against the import being dropped)."""
    import services.orchestrator.main as main_mod

    assert hasattr(main_mod, "local_mode_enabled")
    # Callable and returns a bool regardless of env.
    assert isinstance(main_mod.local_mode_enabled(), bool)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_local_mode.py::test_main_imports_flag_reader -q`
Expected: FAIL — `AttributeError: module 'services.orchestrator.main' has no attribute 'local_mode_enabled'`.

- [ ] **Step 3: Wire the import + startup log**

In `services/orchestrator/main.py`, add `local_mode_enabled` to the existing orchestrator-package import (the line that already reads `from services.orchestrator import call_counter, client_context, ctx_window, events, skill_curator`). Change it to also import the new module symbol by adding a dedicated import beneath that block:

```python
from services.orchestrator.local_mode import local_mode_enabled
```

Then, inside `OrchestratorProcess.run()`, immediately after the existing `_log.info("storage ready")` line, add:

```python
            _log.info(
                "orchestrator mode: %s", "local" if local_mode_enabled() else "pod"
            )
```

(Do not add any behavior branch — this is a log line only.)

- [ ] **Step 4: Run the wiring test + the full suite**

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/test_local_mode.py -q`
Expected: PASS.

Run: `PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q 2>&1 | tail -3`
Expected: `1413 passed` (+ the new local_mode tests) — i.e. `1417 passed` or similar, no failures.

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/main.py tests/services/orchestrator/test_local_mode.py
git commit -m "$(cat <<'EOF'
feat(orchestrator): log resolved LABMATE_LOCAL_MODE at startup

Reads the flag in run() and logs "orchestrator mode: local|pod" — proves
the seam reaches the real entry point. Still no behavior branch.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Seam-map design doc (strangler insertion points)

**Files:**
- Create: `docs/superpowers/specs/2026-07-03-local-harness-seam-map.md`

**Interfaces:**
- Consumes: nothing (documentation).
- Produces: the authoritative insertion-point map every later piece references. No code.

- [ ] **Step 1: Create the seam-map doc**

Create `docs/superpowers/specs/2026-07-03-local-harness-seam-map.md` with exactly this content:

````markdown
# Local-Harness Seam Map (strangler insertion points)

> Companion to `2026-07-03-local-harness-rearchitecture-design.md`. Produced in
> Piece 0. Pins the exact `file:line` where each later piece adds its
> `if local_mode_enabled():` branch. Line numbers are anchors as of the Piece 0
> commit — re-confirm by symbol if they drift.

The flag reader is `services/orchestrator/local_mode.py::local_mode_enabled()`
(default OFF = pod mode). Piece 0 only reads + logs it; no branch yet.

## Seam 1 — LangGraph checkpointer (Piece 1)
- **Insertion point:** `services/orchestrator/graph.py` `build_graph()`, the
  `MongoDBSaver` construction (~lines 982, 1015–1019: `from
  langgraph.checkpoint.mongodb import MongoDBSaver`; `client =
  MongoClient(mongo_uri)`; `cp = MongoDBSaver(client, db_name=db_name)`).
- **Local impl:** `SqliteSaver` from `langgraph-checkpoint-sqlite`, file path
  under the per-user local state dir. Branch on the flag; keep returning
  `(graph, checkpointer)`.
- **Caller to keep intact:** `main.py` `run()` (~line 352) `graph, _cp =
  build_graph(...)`.

## Seam 2 — Sessions / turns / workspaces state (Piece 2, folds in fix-A continuity)
- **Insertion point:** `main.py` `run()` (~line 243) `async with
  StorageManager() as _sm:`, plus `StorageManager.__init__`
  (`storage_manager.py:57–74`) which reads `MONGO_URI/CHROMA_URL/REDIS_URL`.
- **Public interface to preserve** (what the brain calls): `context_manager`,
  `consolidator`, `workspaces`, `loop_checkpoint_collection`,
  `store_episode`, `store_memory`, `close_memory`,
  `boost_memory_importance`, `decay_expired_memories`, `search_memories`,
  `search_turns`, `cache_set`, `cache_get`, `enqueue_task`, and the
  `__aenter__/__aexit__` context-manager contract.
- **Local impl:** SQLite-backed `StorageManager` (sessions, chat_turns,
  workspaces, checkpoints, telemetry). Continuity (`search_turns` /
  `context_manager` reads) becomes a local DB read.
- **Test seam already present:** `StorageManager.from_clients()`
  (`storage_manager.py:76`) + `tests/services/orchestrator/conftest.py`
  `storage` fixture.

## Seam 3 — Chroma / semantic search (Piece 3)
- **Insertion point:** `storage_manager.py::_get_chroma()` (lines 158–161,
  lazy `chromadb.AsyncHttpClient`) feeding `search_memories()` (line 288).
  Consumers: `memory_search.py` (`MemorySearch`), `session_search.py`
  (`SessionSearch`), and the pod codegraph embedder
  (`main.py:265` `pod_codegraph_enabled()`).
- **Local impl:** drop Chroma; `search_memories`/`memory_search` become an
  agentic grep/read + `AGENTS.md` path, or a no-op behind the flag. Optional
  future SQLite-FTS. `session_search` already uses Mongo `$text` → moves with
  Seam 2.

## Seam 4 — Redis (goals / events / steer / cancel / tool-results) (Piece 4, largest)
- **Goals consume:** `main.py` `_loop()` (lines 402–430) `xreadgroup` on
  `labmate:goals` + `xack` (932); `_ensure_group()` (939). Local: goals become
  a direct in-process call into `_handle()` (no stream, no consumer group).
- **Results:** `main.py::_write_result()` (934–937) `SET labmate:result:` +
  `PUBLISH`. Local: direct return / in-proc future.
- **Events:** `events.py::EventEmitter.emit()` (108–126) `xadd
  labmate:events:`. Local: in-process emitter feeding the local gateway.
- **Cancel/steer:** `events.py` `is_cancelled` (139, `EXISTS`), `write_steer`
  (151, `SET EX`), `read_and_clear_steer` (162, `GETDEL`). Local: in-process
  flags/queue keyed by task_id.
- **Cache:** `storage_manager.py` `cache_set/cache_get` (381–388). Local:
  SQLite or in-memory TTL dict.

## Seam 5 — Tool dispatch / delegation (Piece 5, folds in fix-B routing, fixes file-read)
- **Insertion point:** `coding_orchestrator.py` `_run_react_loop` tool dispatch
  — `request_local_tool(...)` calls at lines ~1198 (`run_tests`), ~1281
  (`read_file`/`write_file`/`list_dir`), ~1290 (write-verify read-back). The
  delegation mechanism is `local_tools.py::request_local_tool` (101–155):
  emit `tool.request` → block on `labmate:tool-results:` XREAD.
- **Manifest/threading to drop in local mode:** `tool_manifest.py`
  (`parse_manifest`, `manifest_local_tool_names`), `client_context.py`
  (`current_manifest`, `current_workspace_root` ContextVars), set in
  `main.py:599–605`.
- **Local impl:** `read_file`/`write_file`/`list_dir`/`run_tests` run directly
  on the local FS/shell — no delegation, no manifest, no workspace-root
  threading. **Fixes the file-read mis-route.** fix-B: broaden
  `edit_intent.requires_editing` routing so file-access tasks also enter
  `_run_react_loop` (so the local file tools are reached).

## Seam 6 — Gateway + entry point (Piece 6)
- **Insertion point:** `main.py` `OrchestratorProcess.run()` boot sequence
  (239–396) + the client transports: `ws_gateway/server.py` (419–426 xadd
  goals), `ws_gateway/redis_bridge.py`, `cli/redis_client.py` (33 xadd),
  `cli/ws_client.py`. Local: gateway binds localhost, co-located with the
  in-process orchestrator; frontend points at `ws://localhost`.

## Seam 7 — Packaging / default flip (Piece 7)
- Install script; flip `local_mode_enabled()` default to ON; remove the
  pod/Redis/Mongo/Chroma/delegation paths once local mode is validated.
````

- [ ] **Step 2: Verify the doc renders and links resolve**

Run: `ls -1 docs/superpowers/specs/2026-07-03-local-harness-seam-map.md docs/superpowers/specs/2026-07-03-local-harness-rearchitecture-design.md`
Expected: both paths listed (no "No such file").

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-07-03-local-harness-seam-map.md
git commit -m "$(cat <<'EOF'
docs(arch): local-harness seam map — strangler insertion points (Piece 0)

Pins the file:line where each later migration piece (checkpointer,
sessions, Chroma, Redis, tools, gateway) adds its local_mode_enabled()
branch. Companion to the re-architecture design spec.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Piece-completion gate

After all three tasks:

- [ ] Full suite green: `PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q 2>&1 | tail -3` shows `1413`+new passed, 0 failed.
- [ ] `git status` shows no unintended staged files (never `config.ts` / `daemon.pid`).
- [ ] Open ONE PR for Piece 0 (`feat/local-harness-rearch` already carries the design spec; these commits stack on it). PR body ends with the 🤖 Generated with Claude Code footer.

## Self-review notes (author)

- **Spec coverage:** Piece 0 in the design spec = "confirm/extract the interfaces the brain uses for state, events, and tools … Add `LABMATE_LOCAL_MODE`." Task 1 adds the flag; Task 2 proves it reaches the entry point; Task 3 confirms/extracts the interfaces as the seam map. Covered.
- **No speculative factories:** Piece 0 deliberately adds no single-branch factory/Protocol (YAGNI) — each later piece adds its branch at its own insertion point. The seam map is the extraction artifact.
- **Type consistency:** the only produced symbol is `local_mode_enabled() -> bool`, imported by name in Task 2 and by every later piece.
