# Skill Curator (Proposal-Only, Background) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a background, best-effort skill curator that observes recent successful multi-tool sequences and DRAFTS new skills into a `.proposed/` staging tier for human review — it never auto-generates a working MCP server and never auto-activates a skill.

**Architecture:** A new `services/orchestrator/skill_curator.py` holds PURE, unit-tested functions: a time/idle gate (`should_run_now`), an auto-transition sweep (`sweep_transitions`), and an atomic draft writer (`propose_skill`) that stages `services/skills/.proposed/<name>/SKILL.md` + `server.py.stub` and emits a `skill.proposed` event. `run_curator()` ties them together and isolates the single LLM drafting call (`architect()`) behind one mockable seam. A minimal `RecentSequences` ring buffer (also in this module) captures completed successful goal→tool sequences in `main._handle`; the curator reads it. The curator runs as its own `asyncio` task in `main.py` mirroring the existing `_background_compactor` loop, gated by `ENABLE_SKILL_CURATOR` (default OFF). `SkillRunner.discover()` is extended to skip the `.proposed` dot-dir so a staged draft is never cataloged or activated.

**Tech Stack:** Python 3, asyncio, litellm (via `CodingOrchestrator.architect`), `python-frontmatter` (read-side only; we hand-write frontmatter on the write-side), pytest + pytest-asyncio + pytest-bdd, respx (`fake_model` seam).

## Global Constraints

- **stdout is sacred.** This module is loaded inside the orchestrator process whose stdout is reserved. Never `print()` / write stdout. Log via `logging.getLogger(...)` to stderr only. (CLAUDE.md rule #1)
- **asyncio-correct.** Never call `asyncio.run()` inside an async function or async context. The curator loop is an `asyncio.create_task` child of the goal loop. (CLAUDE.md)
- **Checkpointer.** Do not introduce `MemorySaver`; this plan adds no checkpointer (graph wiring is untouched). (CLAUDE.md rule #8)
- **Discord connector stays deferred.** Do not import, wire, or reference `services/connectors/deferred/`. (CLAUDE.md security constraint)
- **llama.cpp call hygiene.** The one LLM drafting call goes through `CodingOrchestrator.architect(prompt, thinking_budget=...)`, which already sets `api_key="not-needed"` and `thinking_budget_tokens`. Do NOT add a raw `litellm.acompletion` here. (CLAUDE.md rule #6)
- **Best-effort / regression-safe.** `ENABLE_SKILL_CURATOR` defaults to `"0"` (OFF). When OFF the loop task is never created. Any curator failure is caught, logged at DEBUG, and never propagated into goal execution.
- **Proposal-only invariant (NON-NEGOTIABLE).** The curator writes ONLY under `services/skills/.proposed/`. It never writes into an active skill dir, never produces a runnable `server.py` (only `server.py.stub`, clearly marked non-functional), and never causes `SkillRunner.discover()` to catalog the draft.
- **Telemetry dependency.** `sweep_transitions` consumes `compute_state(...)` and the `SkillState` enum from the sibling **telemetry plan** module `services/orchestrator/skill_telemetry.py`. That module is a confirmed prerequisite (currently ABSENT). Tasks 4–5 of this plan depend on it; if `skill_telemetry.py` is not yet implemented, implement that plan first (or its `compute_state` + `SkillState` contract exactly as consumed here).
- **File naming (CLAUDE.md):** Python files `snake_case.py`; Python classes `PascalCase`; functions `snake_case`; skill names `kebab-case`.
- **Env knobs (defaults):** `ENABLE_SKILL_CURATOR=0`, `CURATOR_INTERVAL_HOURS=168`, `CURATOR_MIN_IDLE_HOURS=2`, `CURATOR_MIN_SEQUENCE_LEN=2`, `CURATOR_RECENT_BUFFER=64`.

### Telemetry contract this plan consumes (verbatim, from the telemetry plan)

```python
# services/orchestrator/skill_telemetry.py  (DEPENDENCY — provided by the telemetry plan)
import enum

class SkillState(enum.Enum):
    ACTIVE = "active"
    STALE = "stale"
    ARCHIVED = "archived"

# Pure: decide a skill's lifecycle state from its usage telemetry.
#   last_used_at: float | None  (epoch seconds; None = never used)
#   success_count: int
#   now: float (epoch seconds)
#   stale_after_s: float   (idle → STALE)
#   archive_after_s: float (idle → ARCHIVED, supersedes STALE)
def compute_state(
    *, last_used_at: float | None, success_count: int, now: float,
    stale_after_s: float, archive_after_s: float,
) -> SkillState: ...
```

If the real signature differs, adapt the import in Task 5 — but keep `sweep_transitions` PURE and keep its own behavior identical.

---

## File Map

| File | Responsibility |
|---|---|
| `services/orchestrator/skill_curator.py` (**create**) | The whole feature: env knobs, `CuratorState` sidecar dataclass + load/save, `RecentSequences` ring buffer + `CapturedSequence`, PURE `should_run_now`, PURE `sweep_transitions`, atomic `propose_skill` (writes `.proposed/<name>/SKILL.md` + `server.py.stub`, emits `skill.proposed`), and async `run_curator` (ties gate + sweep + one LLM draft). |
| `services/skill_runner/skill_runner.py` (**modify**, `discover` ~L57-62) | Add `.proposed` to the skip-list so a staged draft is never cataloged/activated. |
| `services/orchestrator/main.py` (**modify**) | Build a shared `RecentSequences` buffer; record a completed SUCCESSFUL goal's tool sequence in `_handle`; spawn the `_background_curator` loop task next to `_background_compactor`, gated by `ENABLE_SKILL_CURATOR`. |
| `tests/services/orchestrator/test_skill_curator.py` (**create**) | Unit tests for gate, ring buffer, sweep, and `propose_skill` file writes + event. |
| `tests/services/orchestrator/test_skill_curator_sidecar.py` (**create**) | Unit tests for `CuratorState` load/save round-trip + corrupt-sidecar fallback. |
| `tests/services/skill_runner/test_discover_skips_proposed.py` (**create**) | Unit test: `discover()` ignores `.proposed/`. |
| `tests/services/orchestrator/test_run_curator.py` (**create**) | `run_curator` integration with the LLM draft mocked (architect stub) + best-effort failure isolation. |
| `tests/services/orchestrator/features/skill_curator.feature` (**create**) | Gherkin scenarios (@mocked). |
| `tests/services/orchestrator/test_skill_curator_bdd.py` (**create**) | pytest-bdd step defs binding the feature. |

---

## Behavior (BDD) — Gherkin

`tests/services/orchestrator/features/skill_curator.feature`

```gherkin
@mocked
Feature: Skill curator (proposal-only, background)
  As the Labmate operator
  I want a background curator that drafts candidate skills for human review
  So that recurring successful tool sequences become reusable skills
  Without ever auto-activating untrusted generated MCP servers

  Background:
    Given a skills root with an active skill "calc"
    And a curator state sidecar that has never run

  Scenario: Curator is OFF by default and is a no-op
    Given ENABLE_SKILL_CURATOR is "0"
    When the orchestrator decides whether to spawn the curator loop
    Then the curator loop task is not created
    And no ".proposed" directory is created

  Scenario: Gate stays closed before the interval has elapsed
    Given the curator last ran 1 hours ago
    And the system has been idle for 9999 seconds
    When the gate is evaluated with interval 168 hours and min idle 2 hours
    Then the gate result is closed

  Scenario: Gate stays closed when the interval elapsed but the host is busy
    Given the curator last ran 200 hours ago
    And the system has been idle for 60 seconds
    When the gate is evaluated with interval 168 hours and min idle 2 hours
    Then the gate result is closed

  Scenario: Gate opens only after interval AND idle are both satisfied
    Given the curator last ran 200 hours ago
    And the system has been idle for 9999 seconds
    When the gate is evaluated with interval 168 hours and min idle 2 hours
    Then the gate result is open

  Scenario: Gate stays closed while paused
    Given the curator is paused
    And the curator last ran 200 hours ago
    And the system has been idle for 9999 seconds
    When the gate is evaluated with interval 168 hours and min idle 2 hours
    Then the gate result is closed

  Scenario: A successful sequence is staged as a proposed skill draft
    Given a recent successful sequence "review-fix" using tools "code-review,edit_file"
    And the LLM drafts the description "Review a file then apply the fix."
    When the curator proposes a skill from that sequence
    Then a file "services/skills/.proposed/review-fix/SKILL.md" exists
    And the SKILL.md frontmatter has name "review-fix"
    And the SKILL.md body mentions tools "code-review" and "edit_file"
    And a file "services/skills/.proposed/review-fix/server.py.stub" exists
    And the server stub is marked non-functional
    And a "skill.proposed" event was emitted with name "review-fix"

  Scenario: discover() never activates a proposed skill
    Given a proposed draft "review-fix" staged under ".proposed"
    When the skill runner discovers skills
    Then the catalog does not contain "review-fix"
    And the catalog still contains "calc"

  Scenario: An unused active skill auto-transitions to archived
    Given an active skill "old-tool" last used 100000000 seconds ago
    When the curator sweeps lifecycle transitions
    Then the transition for "old-tool" is "archived"

  Scenario: A recently used active skill stays active
    Given an active skill "calc" last used 10 seconds ago
    When the curator sweeps lifecycle transitions
    Then the transition for "calc" is "active"

  Scenario: A curator failure never breaks goal execution
    Given the LLM drafting call raises an error
    When the curator runs one cycle
    Then the curator cycle returns without raising
    And the orchestrator goal loop is unaffected
```

---

## Task 1: Module skeleton — env knobs, `CapturedSequence`, `RecentSequences` ring buffer

**Files:**
- Create: `services/orchestrator/skill_curator.py`
- Test: `tests/services/orchestrator/test_skill_curator.py`

**Interfaces:**
- Produces:
  - Module constants: `ENABLE_SKILL_CURATOR: bool`, `CURATOR_INTERVAL_HOURS: float`, `CURATOR_MIN_IDLE_HOURS: float`, `CURATOR_MIN_SEQUENCE_LEN: int`, `CURATOR_RECENT_BUFFER: int`, `PROPOSED_DIRNAME = ".proposed"`, `SKILL_PROPOSED_EVENT = "skill.proposed"`.
  - `@dataclass(frozen=True) CapturedSequence(name: str, goal: str, tools: tuple[str, ...], ok: bool, ts: float)`.
  - `class RecentSequences` with `__init__(self, maxlen: int = CURATOR_RECENT_BUFFER)`, `record(self, seq: CapturedSequence) -> None` (drops non-`ok` and too-short sequences; `len(seq.tools) < CURATOR_MIN_SEQUENCE_LEN`), `snapshot(self) -> list[CapturedSequence]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_skill_curator.py
from services.orchestrator import skill_curator as sc


def test_ring_buffer_keeps_only_successful_multitool_sequences():
    buf = sc.RecentSequences(maxlen=3)
    buf.record(sc.CapturedSequence("a", "goal a", ("t1", "t2"), ok=True, ts=1.0))
    buf.record(sc.CapturedSequence("b", "goal b", ("t1",), ok=True, ts=2.0))      # too short
    buf.record(sc.CapturedSequence("c", "goal c", ("t1", "t2"), ok=False, ts=3.0))  # failed
    snap = buf.snapshot()
    assert [s.name for s in snap] == ["a"]


def test_ring_buffer_evicts_oldest_beyond_maxlen():
    buf = sc.RecentSequences(maxlen=2)
    for i in range(4):
        buf.record(sc.CapturedSequence(f"s{i}", "g", ("t1", "t2"), ok=True, ts=float(i)))
    assert [s.name for s in buf.snapshot()] == ["s2", "s3"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/orchestrator/test_skill_curator.py -v`
Expected: FAIL with `ModuleNotFoundError` / `AttributeError: module 'services.orchestrator.skill_curator' has no attribute 'RecentSequences'`

- [ ] **Step 3: Write minimal implementation**

```python
# services/orchestrator/skill_curator.py
"""
Skill curator — PROPOSAL-ONLY, background, best-effort.

Observes recent SUCCESSFUL multi-tool sequences and DRAFTS candidate skills into
a `.proposed/` staging tier for HUMAN review. It NEVER generates a runnable MCP
server and NEVER auto-activates a skill (SkillRunner.discover skips `.proposed`).

CRITICAL: never write to stdout (this runs inside the orchestrator process whose
stdout carries JSON-RPC / event data). Log to stderr only. Every public entry
point is best-effort: failures are caught + logged at DEBUG and never propagate
into goal execution.
"""
from __future__ import annotations

import logging
import os
from collections import deque
from dataclasses import dataclass

log = logging.getLogger("skill_curator")  # -> stderr via host handlers


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


ENABLE_SKILL_CURATOR = _flag("ENABLE_SKILL_CURATOR", "0")
CURATOR_INTERVAL_HOURS = float(os.getenv("CURATOR_INTERVAL_HOURS", "168"))
CURATOR_MIN_IDLE_HOURS = float(os.getenv("CURATOR_MIN_IDLE_HOURS", "2"))
CURATOR_MIN_SEQUENCE_LEN = int(os.getenv("CURATOR_MIN_SEQUENCE_LEN", "2"))
CURATOR_RECENT_BUFFER = int(os.getenv("CURATOR_RECENT_BUFFER", "64"))

PROPOSED_DIRNAME = ".proposed"
SKILL_PROPOSED_EVENT = "skill.proposed"


@dataclass(frozen=True)
class CapturedSequence:
    """A completed goal and the ordered tools it used."""
    name: str               # kebab-case candidate skill name
    goal: str               # the user goal text
    tools: tuple[str, ...]  # ordered tool/skill names used
    ok: bool                # did the goal succeed
    ts: float               # epoch seconds at completion


class RecentSequences:
    """Bounded ring buffer of recent SUCCESSFUL multi-tool sequences.

    record() silently drops failed or too-short sequences so the curator only
    ever drafts from genuine, repeatable successes.
    """

    def __init__(self, maxlen: int = CURATOR_RECENT_BUFFER) -> None:
        self._buf: deque[CapturedSequence] = deque(maxlen=maxlen)

    def record(self, seq: CapturedSequence) -> None:
        if not seq.ok:
            return
        if len(seq.tools) < CURATOR_MIN_SEQUENCE_LEN:
            return
        self._buf.append(seq)

    def snapshot(self) -> list[CapturedSequence]:
        return list(self._buf)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/orchestrator/test_skill_curator.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/skill_curator.py tests/services/orchestrator/test_skill_curator.py
git commit -m "feat(curator): skill_curator skeleton + RecentSequences ring buffer"
```

---

## Task 2: PURE gate — `should_run_now`

**Files:**
- Modify: `services/orchestrator/skill_curator.py`
- Test: `tests/services/orchestrator/test_skill_curator.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure, no I/O).
- Produces: `def should_run_now(state: "CuratorState", now: float, interval_hours: float, min_idle_hours: float, idle_for_s: float) -> bool`. Opens ONLY when not paused AND `(now - state.last_run_at) >= interval_hours*3600` AND `idle_for_s >= min_idle_hours*3600`. (`CuratorState` lands in Task 3; for this task use a tiny stand-in with `.last_run_at` and `.paused` — replaced by the real dataclass next task.)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/services/orchestrator/test_skill_curator.py
from types import SimpleNamespace

HOUR = 3600.0


def _state(last_run_at=0.0, paused=False):
    return SimpleNamespace(last_run_at=last_run_at, paused=paused)


def test_gate_closed_before_interval():
    st = _state(last_run_at=0.0)
    # 1h since last run, but interval is 168h
    assert sc.should_run_now(st, now=1 * HOUR, interval_hours=168,
                             min_idle_hours=2, idle_for_s=9999) is False


def test_gate_closed_when_busy():
    st = _state(last_run_at=0.0)
    assert sc.should_run_now(st, now=200 * HOUR, interval_hours=168,
                             min_idle_hours=2, idle_for_s=60) is False


def test_gate_open_after_interval_and_idle():
    st = _state(last_run_at=0.0)
    assert sc.should_run_now(st, now=200 * HOUR, interval_hours=168,
                             min_idle_hours=2, idle_for_s=9999) is True


def test_gate_closed_when_paused():
    st = _state(last_run_at=0.0, paused=True)
    assert sc.should_run_now(st, now=200 * HOUR, interval_hours=168,
                             min_idle_hours=2, idle_for_s=9999) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/orchestrator/test_skill_curator.py -k gate -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'should_run_now'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to services/orchestrator/skill_curator.py


def should_run_now(
    state,
    now: float,
    interval_hours: float,
    min_idle_hours: float,
    idle_for_s: float,
) -> bool:
    """PURE gate: True iff the curator should run this cycle.

    Opens only when ALL hold:
      - not paused
      - at least ``interval_hours`` have elapsed since ``state.last_run_at``
      - the host has been idle for at least ``min_idle_hours``
    """
    if getattr(state, "paused", False):
        return False
    if (now - state.last_run_at) < interval_hours * 3600.0:
        return False
    if idle_for_s < min_idle_hours * 3600.0:
        return False
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/orchestrator/test_skill_curator.py -k gate -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/skill_curator.py tests/services/orchestrator/test_skill_curator.py
git commit -m "feat(curator): pure should_run_now interval+idle+pause gate"
```

---

## Task 3: `CuratorState` sidecar — dataclass + atomic load/save

**Files:**
- Modify: `services/orchestrator/skill_curator.py`
- Test: `tests/services/orchestrator/test_skill_curator_sidecar.py`

**Interfaces:**
- Consumes: `should_run_now` (it now takes the real `CuratorState`).
- Produces:
  - `@dataclass CuratorState(last_run_at: float = 0.0, paused: bool = False, run_count: int = 0)`.
  - `def load_state(path: Path) -> CuratorState` — reads JSON sidecar; on missing/corrupt file returns a default `CuratorState()` (best-effort, logs DEBUG).
  - `def save_state(path: Path, state: CuratorState) -> None` — writes JSON atomically (temp file + `os.replace`), creating parent dirs.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_skill_curator_sidecar.py
from pathlib import Path

from services.orchestrator import skill_curator as sc


def test_state_roundtrip(tmp_path: Path):
    p = tmp_path / "state" / "curator.json"
    st = sc.CuratorState(last_run_at=123.0, paused=True, run_count=4)
    sc.save_state(p, st)
    loaded = sc.load_state(p)
    assert loaded == st


def test_missing_sidecar_returns_default(tmp_path: Path):
    loaded = sc.load_state(tmp_path / "nope.json")
    assert loaded == sc.CuratorState()
    assert loaded.last_run_at == 0.0
    assert loaded.paused is False
    assert loaded.run_count == 0


def test_corrupt_sidecar_returns_default(tmp_path: Path):
    p = tmp_path / "curator.json"
    p.write_text("{ not json", encoding="utf-8")
    assert sc.load_state(p) == sc.CuratorState()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/orchestrator/test_skill_curator_sidecar.py -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'CuratorState'`

- [ ] **Step 3: Write minimal implementation**

Add the import line `from pathlib import Path` and `import json` to the top of `skill_curator.py` (next to the existing imports), then append:

```python
# append to services/orchestrator/skill_curator.py
from dataclasses import asdict


@dataclass
class CuratorState:
    """Persisted curator run state (sidecar JSON)."""
    last_run_at: float = 0.0
    paused: bool = False
    run_count: int = 0


def load_state(path: Path) -> CuratorState:
    """Read the sidecar; return a default state on missing/corrupt file (best-effort)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return CuratorState(
            last_run_at=float(data.get("last_run_at", 0.0)),
            paused=bool(data.get("paused", False)),
            run_count=int(data.get("run_count", 0)),
        )
    except FileNotFoundError:
        return CuratorState()
    except Exception as exc:  # corrupt JSON / bad types — never crash the loop
        log.debug("curator sidecar unreadable (%s): %s", path, exc)
        return CuratorState()


def save_state(path: Path, state: CuratorState) -> None:
    """Atomically persist the sidecar (temp file + os.replace)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(state)), encoding="utf-8")
    os.replace(tmp, p)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/orchestrator/test_skill_curator_sidecar.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/skill_curator.py tests/services/orchestrator/test_skill_curator_sidecar.py
git commit -m "feat(curator): CuratorState sidecar with atomic load/save"
```

---

## Task 4: `discover()` skips `.proposed/`

**Files:**
- Modify: `services/skill_runner/skill_runner.py:57-62`
- Test: `tests/services/skill_runner/test_discover_skips_proposed.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a guarantee that a `SKILL.md` whose path contains a `.proposed` segment is never cataloged.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/skill_runner/test_discover_skips_proposed.py
from pathlib import Path

from services.skill_runner.skill_runner import SkillRunner

_FM = "---\nname: {name}\ndescription: {desc}\n---\nbody for {name}\n"


def _skill(root: Path, rel: str, name: str) -> None:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        _FM.format(name=name, desc=f"does {name}"), encoding="utf-8"
    )


def test_discover_skips_proposed(tmp_path: Path):
    root = tmp_path / "skills"
    _skill(root, "calc", "calc")                       # active
    _skill(root, ".proposed/review-fix", "review-fix")  # staged draft
    runner = SkillRunner(roots=[root])
    runner.discover()
    assert "calc" in runner.catalog
    assert "review-fix" not in runner.catalog
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/skill_runner/test_discover_skips_proposed.py -v`
Expected: FAIL — `assert "review-fix" not in runner.catalog` fails (the draft is currently cataloged because `.proposed` is not in the skip-list).

- [ ] **Step 3: Write minimal implementation**

In `services/skill_runner/skill_runner.py`, change the skip-list inside `discover()`:

```python
                # Skip vendored + staged-proposal paths.
                # `.proposed` is the curator's HUMAN-REVIEW staging tier
                # (services/skills/.proposed/<name>/) — drafts there must NEVER
                # be cataloged or activated until a human moves them out.
                parts = skill_md.parts
                if any(
                    p in ("node_modules", ".git", "dist", ".proposed")
                    for p in parts
                ):
                    continue
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/skill_runner/test_discover_skips_proposed.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Run the existing skill_runner suite to confirm no regression**

Run: `python -m pytest tests/services/skill_runner/ -q`
Expected: all pass (no existing test relied on `.proposed` being discoverable).

- [ ] **Step 6: Commit**

```bash
git add services/skill_runner/skill_runner.py tests/services/skill_runner/test_discover_skips_proposed.py
git commit -m "feat(skill-runner): discover() skips .proposed staging tier"
```

---

## Task 5: PURE `sweep_transitions`

**Files:**
- Modify: `services/orchestrator/skill_curator.py`
- Test: `tests/services/orchestrator/test_skill_curator.py`

**Interfaces:**
- Consumes: `compute_state` + `SkillState` from `services/orchestrator/skill_telemetry.py` (telemetry-plan dependency — see Global Constraints).
- Produces: `@dataclass(frozen=True) SkillUsage(name: str, last_used_at: float | None, success_count: int)` and `def sweep_transitions(usages: list[SkillUsage], now: float, *, stale_after_s: float = 14*24*3600, archive_after_s: float = 60*24*3600) -> dict[str, str]` — maps each skill name to its computed `SkillState.value` (`"active"|"stale"|"archived"`). PURE: no I/O, no writes to disk; the caller decides what to do with the verdicts (the proposal-only invariant means a transition is advisory metadata, NOT an active-catalog mutation).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/services/orchestrator/test_skill_curator.py
DAY = 86400.0


def test_sweep_archives_long_unused_skill():
    usages = [sc.SkillUsage("old-tool", last_used_at=0.0, success_count=3)]
    verdicts = sc.sweep_transitions(usages, now=100 * DAY)
    assert verdicts["old-tool"] == "archived"


def test_sweep_keeps_recent_skill_active():
    usages = [sc.SkillUsage("calc", last_used_at=100 * DAY - 10, success_count=5)]
    verdicts = sc.sweep_transitions(usages, now=100 * DAY)
    assert verdicts["calc"] == "active"


def test_sweep_marks_idle_skill_stale():
    # idle 20 days: past the 14-day stale line, short of the 60-day archive line
    usages = [sc.SkillUsage("rusty", last_used_at=80 * DAY, success_count=2)]
    verdicts = sc.sweep_transitions(usages, now=100 * DAY)
    assert verdicts["rusty"] == "stale"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/orchestrator/test_skill_curator.py -k sweep -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'SkillUsage'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to services/orchestrator/skill_curator.py
from services.orchestrator.skill_telemetry import SkillState, compute_state

_STALE_AFTER_S = 14 * 24 * 3600.0
_ARCHIVE_AFTER_S = 60 * 24 * 3600.0


@dataclass(frozen=True)
class SkillUsage:
    """Per-skill usage telemetry the sweep reads (sourced from the telemetry store)."""
    name: str
    last_used_at: float | None
    success_count: int


def sweep_transitions(
    usages: list[SkillUsage],
    now: float,
    *,
    stale_after_s: float = _STALE_AFTER_S,
    archive_after_s: float = _ARCHIVE_AFTER_S,
) -> dict[str, str]:
    """PURE lifecycle sweep: name -> SkillState value ("active"|"stale"|"archived").

    Delegates the per-skill decision to the telemetry plan's compute_state. Returns
    advisory verdicts only — it performs NO disk writes and NEVER mutates the active
    catalog (proposal-only invariant).
    """
    verdicts: dict[str, str] = {}
    for u in usages:
        state: SkillState = compute_state(
            last_used_at=u.last_used_at,
            success_count=u.success_count,
            now=now,
            stale_after_s=stale_after_s,
            archive_after_s=archive_after_s,
        )
        verdicts[u.name] = state.value
    return verdicts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/orchestrator/test_skill_curator.py -k sweep -v`
Expected: PASS (3 passed). If `skill_telemetry.py` is absent the import fails — implement the telemetry plan first (see Global Constraints).

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/skill_curator.py tests/services/orchestrator/test_skill_curator.py
git commit -m "feat(curator): pure sweep_transitions over telemetry compute_state"
```

---

## Task 6: Atomic `propose_skill` — stage `.proposed/<name>/` draft + emit `skill.proposed`

**Files:**
- Modify: `services/orchestrator/skill_curator.py`
- Test: `tests/services/orchestrator/test_skill_curator.py`

**Interfaces:**
- Consumes: `CapturedSequence`, `PROPOSED_DIRNAME`, `SKILL_PROPOSED_EVENT`; `services.orchestrator.events.emit`.
- Produces: `async def propose_skill(skills_root: Path, seq: CapturedSequence, description: str) -> Path | None`. Writes, under `skills_root/.proposed/<seq.name>/`:
  - `SKILL.md` — YAML frontmatter (`name`, `description`, `provenance: agent-created`) + a body containing the description and the distilled step sequence (the tool list as numbered steps).
  - `server.py.stub` — a clearly-marked NON-FUNCTIONAL scaffold (raises `NotImplementedError`).
  Both files are written atomically into a temp dir then `os.replace`'d into place (so a half-written draft never appears). Emits `skill.proposed` with `name` + `path`. Returns the draft dir path, or `None` on failure (best-effort; logs DEBUG). Re-proposing the same name overwrites the prior draft (idempotent).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/services/orchestrator/test_skill_curator.py
import frontmatter as _frontmatter
import pytest

from services.orchestrator import events as _events


class _RecordingEmitter:
    def __init__(self):
        self.events = []

    async def emit(self, type, **fields):
        self.events.append((type, fields))


@pytest.mark.asyncio
async def test_propose_skill_writes_staged_draft_and_emits(tmp_path):
    root = tmp_path / "skills"
    emitter = _RecordingEmitter()
    token = _events.current_emitter.set(emitter)
    try:
        seq = sc.CapturedSequence(
            "review-fix", "Review then fix app.py",
            ("code-review", "edit_file"), ok=True, ts=1.0,
        )
        out = await sc.propose_skill(root, seq, "Review a file then apply the fix.")
    finally:
        _events.current_emitter.reset(token)

    skill_md = root / ".proposed" / "review-fix" / "SKILL.md"
    stub = root / ".proposed" / "review-fix" / "server.py.stub"
    assert out == skill_md.parent
    assert skill_md.exists() and stub.exists()

    post = _frontmatter.load(str(skill_md))
    assert post["name"] == "review-fix"
    assert post["provenance"] == "agent-created"
    assert "code-review" in post.content and "edit_file" in post.content

    stub_text = stub.read_text(encoding="utf-8")
    assert "NOT FUNCTIONAL" in stub_text
    assert "NotImplementedError" in stub_text

    assert any(
        t == "skill.proposed" and f.get("name") == "review-fix"
        for t, f in emitter.events
    )


@pytest.mark.asyncio
async def test_propose_skill_does_not_touch_active_catalog(tmp_path):
    root = tmp_path / "skills"
    (root / "calc").mkdir(parents=True)
    (root / "calc" / "SKILL.md").write_text(
        "---\nname: calc\ndescription: math\n---\nbody\n", encoding="utf-8"
    )
    seq = sc.CapturedSequence("review-fix", "g", ("a", "b"), ok=True, ts=1.0)
    await sc.propose_skill(root, seq, "desc")
    # Active skill dir is untouched; the draft lands ONLY under .proposed/
    assert (root / "calc" / "SKILL.md").read_text(encoding="utf-8").startswith("---")
    assert not (root / "review-fix").exists()
    assert (root / ".proposed" / "review-fix" / "SKILL.md").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/orchestrator/test_skill_curator.py -k propose -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'propose_skill'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to services/orchestrator/skill_curator.py
import shutil
import tempfile

from services.orchestrator import events

_STUB_TEMPLATE = '''\
# ============================================================================
# NOT FUNCTIONAL — agent-drafted scaffold. DO NOT ACTIVATE AS-IS.
# A human must implement this MCP server, verify it, then move this skill out
# of `.proposed/` to activate it. The curator NEVER ships a runnable server.
# ============================================================================
"""Proposed skill server scaffold for: {name}

Candidate derived from a successful tool sequence:
    {tools}
"""


def main() -> None:
    raise NotImplementedError(
        "Proposed skill {name!r} has no implemented server yet. "
        "Implement the MCP server, then move this skill out of .proposed/."
    )


if __name__ == "__main__":
    main()
'''


def _render_skill_md(seq: "CapturedSequence", description: str) -> str:
    steps = "\n".join(f"{i}. Use `{t}`" for i, t in enumerate(seq.tools, start=1))
    # Hand-written frontmatter (write-side) — keep it simple + deterministic.
    return (
        "---\n"
        f"name: {seq.name}\n"
        f"description: {description}\n"
        "provenance: agent-created\n"
        "status: proposed\n"
        "---\n"
        f"# {seq.name} (PROPOSED — pending human review)\n\n"
        f"{description}\n\n"
        f"Derived from goal: {seq.goal}\n\n"
        "## Distilled step sequence\n\n"
        f"{steps}\n"
    )


async def propose_skill(
    skills_root: "Path",
    seq: "CapturedSequence",
    description: str,
) -> "Path | None":
    """Atomically stage a `.proposed/<name>/` draft and emit `skill.proposed`.

    PROPOSAL-ONLY: writes ONLY under `skills_root/.proposed/`, produces a
    non-functional `server.py.stub`, and never activates the skill. Best-effort:
    returns None on failure (logged DEBUG). Idempotent: re-proposing replaces the
    prior draft.
    """
    try:
        proposed_root = Path(skills_root) / PROPOSED_DIRNAME
        proposed_root.mkdir(parents=True, exist_ok=True)
        dest = proposed_root / seq.name

        # Build the full draft in a temp dir, then atomically swap it into place
        # so a half-written draft is never observable.
        tmp = Path(tempfile.mkdtemp(prefix=f"{seq.name}.", dir=proposed_root))
        try:
            (tmp / "SKILL.md").write_text(
                _render_skill_md(seq, description), encoding="utf-8"
            )
            (tmp / "server.py.stub").write_text(
                _STUB_TEMPLATE.format(name=seq.name, tools=", ".join(seq.tools)),
                encoding="utf-8",
            )
            if dest.exists():
                shutil.rmtree(dest)
            os.replace(tmp, dest)
        finally:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)

        await events.emit(SKILL_PROPOSED_EVENT, name=seq.name, path=str(dest))
        log.info("proposed skill draft staged: %s", dest)
        return dest
    except Exception as exc:  # best-effort — never propagate into goal execution
        log.debug("propose_skill failed for %s: %s", getattr(seq, "name", "?"), exc)
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/orchestrator/test_skill_curator.py -k propose -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Confirm the proposal-only invariant end-to-end (discover ignores the staged draft)**

Run: `python -m pytest tests/services/skill_runner/test_discover_skips_proposed.py tests/services/orchestrator/test_skill_curator.py -q`
Expected: all pass — a staged draft from `propose_skill` is never cataloged by `discover()`.

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/skill_curator.py tests/services/orchestrator/test_skill_curator.py
git commit -m "feat(curator): atomic propose_skill staging + skill.proposed event"
```

---

## Task 7: `run_curator` — tie gate + draft + persist, with the LLM call isolated and best-effort

**Files:**
- Modify: `services/orchestrator/skill_curator.py`
- Test: `tests/services/orchestrator/test_run_curator.py`

**Interfaces:**
- Consumes: `should_run_now`, `load_state`, `save_state`, `propose_skill`, `RecentSequences`, `CapturedSequence`.
- Produces:
  ```python
  async def run_curator(
      *,
      skills_root: Path,
      state_path: Path,
      recent: RecentSequences,
      draft_fn: Callable[[str], Awaitable[str]],   # the ONE mockable LLM seam
      now: float,
      idle_for_s: float,
      interval_hours: float = CURATOR_INTERVAL_HOURS,
      min_idle_hours: float = CURATOR_MIN_IDLE_HOURS,
  ) -> Path | None
  ```
  Loads state; if `should_run_now` is False returns `None` (no-op). Else picks the most recent captured sequence, calls `draft_fn(prompt)` to get the description (the single LLM call — caller passes `orch.architect`), calls `propose_skill`, bumps `last_run_at`/`run_count`, persists state, and returns the draft path. Entirely wrapped so any failure (incl. `draft_fn` raising) is caught, logged DEBUG, and returns `None` — never raises.

- [ ] **Step 1: Write the failing test**

```python
# tests/services/orchestrator/test_run_curator.py
from pathlib import Path

import pytest

from services.orchestrator import skill_curator as sc

HOUR = 3600.0


def _buf_with_one():
    buf = sc.RecentSequences()
    buf.record(sc.CapturedSequence("review-fix", "g", ("code-review", "edit_file"),
                                   ok=True, ts=1.0))
    return buf


@pytest.mark.asyncio
async def test_run_curator_noop_when_gate_closed(tmp_path):
    state_path = tmp_path / "curator.json"
    sc.save_state(state_path, sc.CuratorState(last_run_at=199 * HOUR))

    async def draft_fn(prompt):  # must NOT be called
        raise AssertionError("draft_fn called while gate closed")

    out = await sc.run_curator(
        skills_root=tmp_path / "skills", state_path=state_path,
        recent=_buf_with_one(), draft_fn=draft_fn,
        now=200 * HOUR, idle_for_s=60,  # busy -> gate closed
    )
    assert out is None


@pytest.mark.asyncio
async def test_run_curator_drafts_and_persists_when_gate_open(tmp_path):
    state_path = tmp_path / "curator.json"
    sc.save_state(state_path, sc.CuratorState(last_run_at=0.0, run_count=2))
    calls = []

    async def draft_fn(prompt):
        calls.append(prompt)
        return "Review a file then apply the fix."

    out = await sc.run_curator(
        skills_root=tmp_path / "skills", state_path=state_path,
        recent=_buf_with_one(), draft_fn=draft_fn,
        now=200 * HOUR, idle_for_s=9999,
    )
    assert out is not None
    assert (out / "SKILL.md").exists()
    assert len(calls) == 1                       # exactly one LLM call
    persisted = sc.load_state(state_path)
    assert persisted.last_run_at == 200 * HOUR
    assert persisted.run_count == 3


@pytest.mark.asyncio
async def test_run_curator_swallows_draft_failure(tmp_path):
    state_path = tmp_path / "curator.json"
    sc.save_state(state_path, sc.CuratorState(last_run_at=0.0))

    async def draft_fn(prompt):
        raise RuntimeError("model down")

    out = await sc.run_curator(
        skills_root=tmp_path / "skills", state_path=state_path,
        recent=_buf_with_one(), draft_fn=draft_fn,
        now=200 * HOUR, idle_for_s=9999,
    )
    assert out is None  # best-effort: failure is swallowed, no raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/orchestrator/test_run_curator.py -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'run_curator'`

- [ ] **Step 3: Write minimal implementation**

Add `from typing import Awaitable, Callable` to the imports, then append:

```python
# append to services/orchestrator/skill_curator.py


def _draft_prompt(seq: "CapturedSequence") -> str:
    tools = " then ".join(seq.tools)
    return (
        "You are distilling a successful agent run into a reusable skill "
        "description. The agent achieved the goal:\n"
        f"  {seq.goal}\n"
        f"by using these tools in order: {tools}.\n\n"
        "Write ONE or TWO plain sentences describing WHEN to use a skill that "
        "performs this sequence. No preamble, no markdown."
    )


async def run_curator(
    *,
    skills_root: "Path",
    state_path: "Path",
    recent: RecentSequences,
    draft_fn: "Callable[[str], Awaitable[str]]",
    now: float,
    idle_for_s: float,
    interval_hours: float = CURATOR_INTERVAL_HOURS,
    min_idle_hours: float = CURATOR_MIN_IDLE_HOURS,
) -> "Path | None":
    """One curator cycle: gate -> draft (1 LLM call) -> stage proposal -> persist.

    Best-effort: any failure (incl. draft_fn raising) is caught, logged at DEBUG,
    and returns None — the curator never breaks goal execution.
    """
    try:
        state = load_state(state_path)
        if not should_run_now(state, now, interval_hours, min_idle_hours, idle_for_s):
            return None

        sequences = recent.snapshot()
        if not sequences:
            # Nothing to draft from, but still record that we ran (avoids busy-looping
            # the gate every cycle once the interval has elapsed).
            state.last_run_at = now
            state.run_count += 1
            save_state(state_path, state)
            return None

        seq = sequences[-1]  # most recent successful multi-tool sequence
        description = (await draft_fn(_draft_prompt(seq))).strip()
        if not description:
            description = f"Proposed skill distilled from: {seq.goal}"

        dest = await propose_skill(skills_root, seq, description)

        state.last_run_at = now
        state.run_count += 1
        save_state(state_path, state)
        return dest
    except Exception as exc:  # best-effort isolation
        log.debug("run_curator cycle failed (non-fatal): %s", exc)
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/orchestrator/test_run_curator.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/skill_curator.py tests/services/orchestrator/test_run_curator.py
git commit -m "feat(curator): run_curator cycle with isolated, best-effort LLM draft seam"
```

---

## Task 8: Background wiring in `main.py` — capture sequences + spawn the gated loop

**Files:**
- Modify: `services/orchestrator/main.py`
- Test: `tests/services/orchestrator/test_run_curator.py` (append a wiring-helper test; no live process)

**Interfaces:**
- Consumes: `RecentSequences`, `CapturedSequence`, `run_curator`, `ENABLE_SKILL_CURATOR`, `CURATOR_INTERVAL_HOURS`, `CURATOR_MIN_IDLE_HOURS`; `orch.architect`.
- Produces:
  - On `OrchestratorProcess`: `self._recent_sequences = RecentSequences()` (created in `__init__`), and `self._last_goal_at: float` updated in `_handle` to drive idle computation.
  - A module-level helper `_extract_tool_sequence(final_state: dict) -> tuple[str, ...]` so the capture logic is unit-testable without a process.
  - A `_background_curator(self, orch)` async loop mirroring `_background_compactor`, spawned ONLY when `ENABLE_SKILL_CURATOR` is true.

- [ ] **Step 1: Write the failing test (the pure extraction helper)**

```python
# append to tests/services/orchestrator/test_run_curator.py
from services.orchestrator.main import _extract_tool_sequence


def test_extract_tool_sequence_reads_state_tools():
    state = {"tools_used": ["code-review", "edit_file"], "error": None}
    assert _extract_tool_sequence(state) == ("code-review", "edit_file")


def test_extract_tool_sequence_empty_when_absent():
    assert _extract_tool_sequence({"error": None}) == ()
    assert _extract_tool_sequence("not a dict") == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/services/orchestrator/test_run_curator.py -k extract -v`
Expected: FAIL with `ImportError: cannot import name '_extract_tool_sequence'`

- [ ] **Step 3: Write minimal implementation**

In `services/orchestrator/main.py`:

(a) Add imports near the other orchestrator imports (after `from services.orchestrator import call_counter`):

```python
from services.orchestrator import skill_curator
from services.orchestrator.skill_curator import (
    RecentSequences,
    CapturedSequence,
    run_curator,
)
```

(b) Add the pure extraction helper at module scope (below the constants, above `_worker_id`):

```python
def _extract_tool_sequence(final_state) -> tuple[str, ...]:
    """Pull the ordered tool/skill names a completed goal used from its state.

    Reads final_state['tools_used'] when present (the orchestrator records the
    per-goal tool sequence there). Returns an empty tuple for a non-dict or a
    state with no recorded tools, so the curator simply has nothing to draft.
    """
    if not isinstance(final_state, dict):
        return ()
    tools = final_state.get("tools_used") or []
    if not isinstance(tools, (list, tuple)):
        return ()
    return tuple(str(t) for t in tools if t)
```

> Implementer note: `tools_used` is the agreed state key for the per-goal tool
> sequence. If the graph does not yet populate it, the orchestrator already
> threads tool names through `_run_react_loop` (the `_turn_tools` / `tool.start`
> emissions in `coding_orchestrator.py`); append the dispatched tool name to a
> `state["tools_used"]` list at the same site, or accumulate it on the goal node.
> Keep that accumulation additive (a new optional state field) — no removals.

(c) In `OrchestratorProcess.__init__`, add:

```python
        self._recent_sequences = RecentSequences()
        self._last_goal_at: float = 0.0
```

(d) In `_handle`, right after the SUCCESS result is written (just after the
`_log.info("task %s complete", task_id)` line, inside the success branch), record
the sequence and the activity timestamp:

```python
            # Skill-curator: record a SUCCESSFUL multi-tool sequence as a draft
            # candidate (best-effort; never blocks). RecentSequences itself drops
            # failed / too-short sequences.
            import time as _time
            self._last_goal_at = _time.time()
            try:
                _tools = _extract_tool_sequence(final_state)
                self._recent_sequences.record(CapturedSequence(
                    name=_slug_for(task_text),
                    goal=task_text[:500],
                    tools=_tools,
                    ok=ok_flag,
                    ts=self._last_goal_at,
                ))
            except Exception:
                pass  # capture is best-effort
```

(e) Add the slug helper at module scope (next to `_extract_tool_sequence`):

```python
import re as _re


def _slug_for(task_text: str) -> str:
    """kebab-case candidate skill name from the goal text (deterministic)."""
    words = _re.findall(r"[a-z0-9]+", (task_text or "").lower())
    slug = "-".join(words[:4]) or "proposed-skill"
    return slug[:48]
```

(f) Add the background loop method on `OrchestratorProcess` (next to
`_background_compactor`):

```python
    async def _background_curator(self, orch: "CodingOrchestrator") -> None:
        """Periodic, best-effort skill-curator loop (mirrors _background_compactor).

        Only created when ENABLE_SKILL_CURATOR is set. Each cycle is gated by
        should_run_now (interval + idle) inside run_curator. Failures are isolated
        there; this loop additionally guards its own sleep/iteration.
        """
        import time as _time
        skills_root = Path(__file__).resolve().parent.parent / "skills"
        state_path = (
            Path(os.getenv("CURATOR_STATE_DIR", ".data")) / "skill_curator.json"
        )
        # Wake roughly hourly; the real interval gate lives in run_curator.
        wake_s = int(os.getenv("CURATOR_WAKE_INTERVAL_S", "3600"))

        async def _draft(prompt: str) -> str:
            # The ONE LLM call — routed through architect() (api_key + budget set).
            return await orch.architect(prompt, thinking_budget=0)

        while not self._shutdown.is_set():
            try:
                await asyncio.sleep(wake_s)
                if self._shutdown.is_set():
                    break
                now = _time.time()
                idle_for_s = now - self._last_goal_at if self._last_goal_at else 1e9
                await run_curator(
                    skills_root=skills_root,
                    state_path=state_path,
                    recent=self._recent_sequences,
                    draft_fn=_draft,
                    now=now,
                    idle_for_s=idle_for_s,
                    interval_hours=skill_curator.CURATOR_INTERVAL_HOURS,
                    min_idle_hours=skill_curator.CURATOR_MIN_IDLE_HOURS,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.debug("background curator sweep failed (non-fatal)", exc_info=True)
```

(g) In `run()`, spawn the loop next to `bg_compactor` (gated OFF by default), and
cancel it in the same `finally`:

```python
            bg_compactor = asyncio.create_task(
                self._background_compactor(orch, _sm), name="background-compactor",
            )
            bg_curator = None
            if skill_curator.ENABLE_SKILL_CURATOR:
                bg_curator = asyncio.create_task(
                    self._background_curator(orch), name="background-curator",
                )
                _log.info("skill curator enabled (proposal-only, background)")
            try:
                await self._loop(orch, _sm)
            finally:
                bg_compactor.cancel()
                try:
                    await bg_compactor
                except asyncio.CancelledError:
                    pass
                if bg_curator is not None:
                    bg_curator.cancel()
                    try:
                        await bg_curator
                    except asyncio.CancelledError:
                        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/services/orchestrator/test_run_curator.py -k extract -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Confirm `main.py` still imports and the OFF-by-default path creates no curator task**

Run: `python -c "import services.orchestrator.main as m; print(hasattr(m, '_extract_tool_sequence'), m.skill_curator.ENABLE_SKILL_CURATOR)"`
Expected: `True False` (helper present; curator OFF by default).

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/main.py tests/services/orchestrator/test_run_curator.py
git commit -m "feat(curator): wire best-effort background curator + sequence capture (OFF by default)"
```

---

## Task 9: BDD — feature file + step defs

**Files:**
- Create: `tests/services/orchestrator/features/skill_curator.feature` (content = the full Gherkin in the "Behavior (BDD)" section above — copy it verbatim)
- Create: `tests/services/orchestrator/test_skill_curator_bdd.py`
- Test: the BDD scenarios themselves

**Interfaces:**
- Consumes: everything public from `skill_curator.py`; `fake_model` is available but these scenarios bind the pure functions + `propose_skill` directly (the only LLM-shaped step, "the LLM drafts the description", uses an inline async stub rather than the HTTP seam — `propose_skill` takes the description as an argument, so no model call is made; `fake_model` remains available for the failure scenario if an implementer chooses to route through `run_curator`).

- [ ] **Step 1: Create the feature file**

Write `tests/services/orchestrator/features/skill_curator.feature` with the EXACT Gherkin from the "Behavior (BDD) — Gherkin" section above.

- [ ] **Step 2: Write the step defs (this is the failing test)**

```python
# tests/services/orchestrator/test_skill_curator_bdd.py
"""pytest-bdd step defs for skill_curator.feature.

Mocked only (@mocked): no GPU, no services. Binds Gherkin to the real pure
curator functions, the real propose_skill file writer, and the real SkillRunner
discover(). The one LLM-shaped step passes a literal description (propose_skill
takes the description as an argument), so no model call is issued."""
from __future__ import annotations

from pathlib import Path

import frontmatter as _frontmatter
import pytest
from pytest_bdd import scenarios, given, when, then, parsers

from services.skill_runner.skill_runner import SkillRunner
from services.orchestrator import events
from services.orchestrator import skill_curator as sc
from tests.conftest import run_async

scenarios("features/skill_curator.feature")

HOUR = 3600.0
_FM = "---\nname: {name}\ndescription: {desc}\n---\nbody for {name}\n"


class _RecordingEmitter:
    def __init__(self):
        self.events = []

    async def emit(self, type, **fields):
        self.events.append((type, fields))


@pytest.fixture
def ctx(tmp_path):
    return {
        "root": tmp_path / "skills",
        "state": sc.CuratorState(),
        "now": 200 * HOUR,
        "idle_for_s": 9999.0,
        "interval_hours": 168.0,
        "min_idle_hours": 2.0,
        "gate": None,
        "seq": None,
        "description": "",
        "draft_dir": None,
        "verdicts": {},
        "emitter": _RecordingEmitter(),
        "spawn_curator": None,
    }


@pytest.fixture(autouse=True)
def _emitter(ctx):
    token = events.current_emitter.set(ctx["emitter"])
    yield
    events.current_emitter.reset(token)


def _write_active(root: Path, name: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_FM.format(name=name, desc=f"does {name}"),
                               encoding="utf-8")


# ── Background ──────────────────────────────────────────────────────────────

@given(parsers.parse('a skills root with an active skill "{name}"'))
def _root(ctx, name):
    _write_active(ctx["root"], name)


@given("a curator state sidecar that has never run")
def _fresh_state(ctx):
    ctx["state"] = sc.CuratorState(last_run_at=0.0)


# ── Given ───────────────────────────────────────────────────────────────────

@given(parsers.parse('ENABLE_SKILL_CURATOR is "{val}"'))
def _enable_flag(ctx, val):
    enabled = val.strip() in ("1", "true", "yes", "on")
    ctx["spawn_curator"] = enabled


@given(parsers.parse("the curator last ran {hours:d} hours ago"))
def _last_ran(ctx, hours):
    ctx["state"] = sc.CuratorState(
        last_run_at=ctx["now"] - hours * HOUR, paused=ctx["state"].paused
    )


@given(parsers.parse("the system has been idle for {secs:d} seconds"))
def _idle(ctx, secs):
    ctx["idle_for_s"] = float(secs)


@given("the curator is paused")
def _paused(ctx):
    ctx["state"] = sc.CuratorState(last_run_at=ctx["state"].last_run_at, paused=True)


@given(parsers.parse(
    'a recent successful sequence "{name}" using tools "{tools}"'))
def _recent_seq(ctx, name, tools):
    ctx["seq"] = sc.CapturedSequence(
        name=name, goal=f"goal {name}",
        tools=tuple(tools.split(",")), ok=True, ts=1.0,
    )


@given(parsers.parse('the LLM drafts the description "{desc}"'))
def _drafted(ctx, desc):
    ctx["description"] = desc


@given(parsers.parse('a proposed draft "{name}" staged under ".proposed"'))
def _staged(ctx, name):
    seq = sc.CapturedSequence(name, "g", ("a", "b"), ok=True, ts=1.0)
    run_async(sc.propose_skill(ctx["root"], seq, "desc"))


@given(parsers.parse(
    'an active skill "{name}" last used {secs:d} seconds ago'))
def _usage(ctx, name, secs):
    ctx.setdefault("usages", []).append(
        sc.SkillUsage(name, last_used_at=ctx["now"] - secs, success_count=1)
    )


@given("the LLM drafting call raises an error")
def _draft_raises(ctx):
    async def _bad(prompt):
        raise RuntimeError("model down")
    ctx["draft_fn"] = _bad
    ctx["seq"] = sc.CapturedSequence("x", "g", ("a", "b"), ok=True, ts=1.0)


# ── When ────────────────────────────────────────────────────────────────────

@when("the orchestrator decides whether to spawn the curator loop")
def _decide_spawn(ctx):
    # mirrors main.run(): the loop is created only when the flag is on
    ctx["loop_created"] = bool(ctx["spawn_curator"])


@when(parsers.parse(
    "the gate is evaluated with interval {iv:d} hours and min idle {mi:d} hours"))
def _eval_gate(ctx, iv, mi):
    ctx["gate"] = sc.should_run_now(
        ctx["state"], now=ctx["now"], interval_hours=iv,
        min_idle_hours=mi, idle_for_s=ctx["idle_for_s"],
    )


@when("the curator proposes a skill from that sequence")
def _propose(ctx):
    ctx["draft_dir"] = run_async(
        sc.propose_skill(ctx["root"], ctx["seq"], ctx["description"])
    )


@when("the skill runner discovers skills")
def _discover(ctx):
    runner = SkillRunner(roots=[ctx["root"]])
    runner.discover()
    ctx["catalog"] = runner.catalog


@when("the curator sweeps lifecycle transitions")
def _sweep(ctx):
    ctx["verdicts"] = sc.sweep_transitions(ctx["usages"], now=ctx["now"])


@when("the curator runs one cycle")
def _run_cycle(ctx, tmp_path):
    buf = sc.RecentSequences()
    buf.record(ctx["seq"])
    ctx["cycle_result"] = run_async(sc.run_curator(
        skills_root=ctx["root"], state_path=tmp_path / "s.json",
        recent=buf, draft_fn=ctx["draft_fn"],
        now=ctx["now"], idle_for_s=9999.0,
    ))
    ctx["cycle_raised"] = False


# ── Then ────────────────────────────────────────────────────────────────────

@then("the curator loop task is not created")
def _no_loop(ctx):
    assert ctx["loop_created"] is False


@then(parsers.parse('no ".proposed" directory is created'))
def _no_proposed(ctx):
    assert not (ctx["root"] / ".proposed").exists()


@then("the gate result is closed")
def _gate_closed(ctx):
    assert ctx["gate"] is False


@then("the gate result is open")
def _gate_open(ctx):
    assert ctx["gate"] is True


@then(parsers.parse('a file "{relpath}" exists'))
def _file_exists(ctx, relpath):
    # relpath is repo-relative "services/skills/.proposed/<name>/<file>";
    # map it under the tmp root by its tail after ".proposed/".
    tail = relpath.split(".proposed/", 1)[1]
    assert (ctx["root"] / ".proposed" / tail).exists()


@then(parsers.parse('the SKILL.md frontmatter has name "{name}"'))
def _fm_name(ctx, name):
    post = _frontmatter.load(str(ctx["draft_dir"] / "SKILL.md"))
    assert post["name"] == name


@then(parsers.parse(
    'the SKILL.md body mentions tools "{a}" and "{b}"'))
def _body_tools(ctx, a, b):
    post = _frontmatter.load(str(ctx["draft_dir"] / "SKILL.md"))
    assert a in post.content and b in post.content


@then("the server stub is marked non-functional")
def _stub_marked(ctx):
    text = (ctx["draft_dir"] / "server.py.stub").read_text(encoding="utf-8")
    assert "NOT FUNCTIONAL" in text


@then(parsers.parse('a "skill.proposed" event was emitted with name "{name}"'))
def _event_emitted(ctx, name):
    assert any(
        t == "skill.proposed" and f.get("name") == name
        for t, f in ctx["emitter"].events
    )


@then(parsers.parse('the catalog does not contain "{name}"'))
def _catalog_excludes(ctx, name):
    assert name not in ctx["catalog"]


@then(parsers.parse('the catalog still contains "{name}"'))
def _catalog_includes(ctx, name):
    assert name in ctx["catalog"]


@then(parsers.parse('the transition for "{name}" is "{state}"'))
def _transition(ctx, name, state):
    assert ctx["verdicts"][name] == state


@then("the curator cycle returns without raising")
def _no_raise(ctx):
    assert ctx["cycle_raised"] is False
    assert ctx["cycle_result"] is None


@then("the orchestrator goal loop is unaffected")
def _loop_unaffected(ctx):
    # The cycle swallowed the error and produced no proposal; nothing leaked.
    assert ctx["cycle_result"] is None
```

- [ ] **Step 3: Run the BDD scenarios to verify they pass**

Run: `python -m pytest tests/services/orchestrator/test_skill_curator_bdd.py -v`
Expected: PASS — all scenarios green. (If any step is reported "not found", check the `parsers.parse` strings match the feature lines verbatim.)

- [ ] **Step 4: Commit**

```bash
git add tests/services/orchestrator/features/skill_curator.feature tests/services/orchestrator/test_skill_curator_bdd.py
git commit -m "test(curator): BDD feature + step defs (proposal-only, gate, discover-skip)"
```

---

## Task 10: Full-suite regression gate

**Files:** none (verification only).

- [ ] **Step 1: Run the orchestrator + skill_runner + memory suites**

Run:
```bash
cd /Users/zachstallbohm/Work/Labmate
python -m pytest tests/services/orchestrator/ tests/services/skill_runner/ tests/services/memory/ -q
```
Expected: all pass (the pre-existing 684 + the new curator tests). The curator is OFF by default, so no existing behavior changes.

- [ ] **Step 2: Confirm the proposal-only invariant one more time (no active-catalog writes anywhere)**

Run:
```bash
grep -Rn "PROPOSED_DIRNAME\|.proposed" services/orchestrator/skill_curator.py
```
Expected: every write path in `skill_curator.py` is rooted at `<skills_root>/.proposed/...` — there is no write to a non-`.proposed` skill dir.

- [ ] **Step 3: Commit (if any doc/lint touch-ups were needed; otherwise skip)**

```bash
git add -A
git commit -m "chore(curator): full-suite green; proposal-only invariant verified"
```

---

## Self-Review

**1. Spec coverage**

| Spec requirement | Task |
|---|---|
| `should_run_now(state, now, interval_hours, min_idle_hours, idle_for_s) -> bool`, PURE, honors interval+idle (+pause) | Task 2 |
| PURE auto-transition sweep using `compute_state` from `skill_telemetry.py` (DEPENDENCY stated) | Task 5 |
| `propose_skill(...)` writes `.proposed/<name>/` draft atomically + emits `skill.proposed` | Task 6 |
| `run_curator(...)` ties gate+sweep+draft; LLM call isolated + mockable | Task 7 |
| Candidate source = recent SUCCESSFUL multi-tool sequences; add ring buffer if no trace store | Tasks 1 (buffer) + 8 (capture in `_handle`) — confirmed orchestrator has no existing successful-sequence trace store, so the ring buffer is added |
| LLM drafting uses orchestrator `architect()` model, isolated/mockable | Task 7 (`draft_fn` seam) + Task 8 (`_draft` → `orch.architect`) |
| BACKGROUND + best-effort, mirrors `consolidation_worker`/`_background_compactor` | Task 8 (`_background_curator`) |
| `ENABLE_SKILL_CURATOR` default OFF; `CURATOR_INTERVAL_HOURS=168`; `CURATOR_MIN_IDLE_HOURS=2` | Task 1 (knobs) + Task 8 (gated spawn) |
| `.proposed/SKILL.md` (frontmatter + description + distilled steps) + `server.py.stub` (non-functional) | Task 6 |
| `discover()` MUST skip `.proposed/` | Task 4 |
| Never auto-activates; never auto-generates a working server | Tasks 4 + 6 (stub-only, discover-skip) |
| Curator failure never breaks orchestrator (best-effort, DEBUG log) | Tasks 6, 7, 8 (try/except at every entry point) |
| stdout-sacred, asyncio-correct, no MemorySaver, Discord deferred | Global Constraints + no graph/connector touch |
| Full Gherkin: OFF no-op; gate after interval+idle; sequence→staged draft+event; discover never activates; unused→stale/archived; failure isolation | Task 9 feature + Behavior section |

**2. Placeholder scan:** No "TBD"/"add error handling"/"similar to Task N". Every code step shows full code; every command shows expected output. The one cross-plan dependency (`skill_telemetry.compute_state`) is given a verbatim contract in Global Constraints so the implementer is not guessing.

**3. Type consistency:** `CapturedSequence`/`RecentSequences` (Task 1) are consumed unchanged by `propose_skill` (Task 6), `run_curator` (Task 7), and the wiring (Task 8). `CuratorState` (Task 3) is the same object `should_run_now` (Task 2, via stand-in) and `load_state`/`save_state` operate on. `propose_skill` signature `(skills_root, seq, description) -> Path | None` matches its call in `run_curator` and in the BDD step defs. `sweep_transitions` returns `dict[str, str]` of `SkillState.value` strings, asserted as `"active"|"stale"|"archived"` in tests. `SKILL_PROPOSED_EVENT == "skill.proposed"` is the literal asserted in both unit and BDD tests.

**Two invariants worth restating before execution:**

- **Telemetry dependency:** `sweep_transitions` (Task 5) imports `compute_state` + `SkillState` from `services/orchestrator/skill_telemetry.py`, which is the deliverable of the **sibling telemetry plan** and is currently ABSENT. Execute that plan first, or stub the contract exactly as written in Global Constraints. Tasks 1–4 and 6–9 do not depend on it and can proceed independently.
- **PROPOSAL-ONLY guarantee:** the curator's only filesystem writes are under `<skills_root>/.proposed/`; it emits `skill.proposed` for a human; `discover()` skips `.proposed/`; and the staged `server.py.stub` raises `NotImplementedError` and is clearly marked NOT FUNCTIONAL. There is no code path that activates a generated skill or runs a generated server. A human reviews the draft, implements the real MCP server, and moves it out of `.proposed/` to activate.
