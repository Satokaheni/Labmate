# Repeat-Analysis Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** Short-circuit a repeat call to a read-only analysis skill (code-review/critique) on an
already-analyzed target during the ReAct loop, steering the model to edit instead of re-review.

**Architecture:** One pure helper module (`repeat_analysis_guard.py`, mirroring
`load_skill_guard.py`) + a small wire-in to `coding_orchestrator.py`'s `call_skill_tool` dispatch.
Flag-gated `ENABLE_REPEAT_ANALYSIS_GUARD`, **default OFF ⇒ byte-identical to today**.

**Tech stack:** Python, pytest + pytest-asyncio. Spec: `docs/superpowers/specs/2026-07-03-repeat-analysis-guard-design.md`.

## Global Constraints
- **Default OFF, behavior-preserving.** When the flag is off, the `call_skill_tool` path must be
  unchanged (skill executes as today). Verified by a test.
- Touch only: `services/orchestrator/repeat_analysis_guard.py` (new),
  `services/orchestrator/coding_orchestrator.py`, `tests/services/orchestrator/…`, `CLAUDE.md`.
- Guard read-only analysis skills only — NEVER `code-sandbox`/`run_tests` (legit re-runs).
- stdout is sacred in MCP servers — but `coding_orchestrator.py` is not an MCP server; still, no
  new prints (use the existing `events.emit`).
- Run tests from repo root: `PYTHONPATH=. python -m pytest <path> -v`.
- Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- Stage by exact path — never `git add -A`; never stage `services/frontend/src/config.ts` or `.codegraph/daemon.pid`.

---

## Task 1: `repeat_analysis_guard.py` pure module

**Files:** Create `services/orchestrator/repeat_analysis_guard.py`; Test `tests/services/orchestrator/test_repeat_analysis_guard.py`

**Interfaces produced:** `analysis_skills() -> frozenset[str]`, `repeat_analysis_guard_enabled() -> bool`,
`is_guarded_analysis(skill) -> bool`, `analysis_key(skill, arguments) -> str`, `build_analysis_steer(skill, key) -> dict`

- [ ] **Step 1: Write the failing tests**

```python
# tests/services/orchestrator/test_repeat_analysis_guard.py
from services.orchestrator.repeat_analysis_guard import (
    analysis_key, build_analysis_steer, is_guarded_analysis, repeat_analysis_guard_enabled,
)


def test_default_guarded_skills_include_review_not_sandbox():
    assert is_guarded_analysis("code-review")
    assert is_guarded_analysis("critique")
    assert not is_guarded_analysis("code-sandbox")   # re-running after an edit is legit
    assert not is_guarded_analysis("run_tests")


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("ENABLE_REPEAT_ANALYSIS_GUARD", raising=False)
    assert repeat_analysis_guard_enabled() is False
    monkeypatch.setenv("ENABLE_REPEAT_ANALYSIS_GUARD", "1")
    assert repeat_analysis_guard_enabled() is True


def test_key_same_target_ignores_reworded_args():
    a = analysis_key("code-review", {"file": "ab_buggy.py"})
    b = analysis_key("code-review", {"file": "ab_buggy.py", "prompt": "look again more carefully"})
    assert a == b   # defeats the arg-variation evasion the LoopDetector suffers from


def test_key_different_target_differs():
    assert analysis_key("code-review", {"file": "a.py"}) != analysis_key("code-review", {"file": "b.py"})


def test_key_no_target_falls_back_to_skill_only():
    assert analysis_key("critique", {"prompt": "x"}) == "critique"


def test_env_override_of_guarded_set(monkeypatch):
    monkeypatch.setenv("REPEAT_ANALYSIS_SKILLS", "repo-fault-localize, code-review")
    assert is_guarded_analysis("repo-fault-localize")
    assert not is_guarded_analysis("critique")


def test_steer_names_skill_and_target_and_points_to_edit():
    obs = build_analysis_steer("code-review", "code-review::ab_buggy.py")
    assert obs["response"]["status"] == "already_analyzed"
    msg = obs["response"]["message"].lower()
    assert "code-review" in msg and "ab_buggy.py" in msg
    assert "write_file" in msg or "edit" in msg
```

- [ ] **Step 2: Run — verify fail**

`PYTHONPATH=. python -m pytest tests/services/orchestrator/test_repeat_analysis_guard.py -v`
Expected: FAIL (`ModuleNotFoundError: services.orchestrator.repeat_analysis_guard`).

- [ ] **Step 3: Implement the module**

```python
# services/orchestrator/repeat_analysis_guard.py
"""Guard against re-running a read-only ANALYSIS skill (code-review, critique, …) on a target
the ReAct loop already analyzed this goal. Mirrors load_skill_guard.py, but for call_skill_tool:
a re-review is short-circuited with a steer toward the edit instead of burning another iteration.
Flag-gated (ENABLE_REPEAT_ANALYSIS_GUARD), default OFF ⇒ behavior-preserving.
"""
import os

# Read-only "produce a diagnosis" skills whose RE-invocation on the same target is churn.
# NOT code-sandbox / run_tests — re-running those after an edit is legit verification.
_DEFAULT_ANALYSIS_SKILLS = frozenset({"code-review", "critique", "design-critique"})

# arg fields that commonly carry the analyzed target (best-effort).
_TARGET_ARG_KEYS = ("file", "path", "filename", "target", "file_path")


def analysis_skills() -> frozenset[str]:
    """Guarded analysis skills; override via REPEAT_ANALYSIS_SKILLS=comma,list."""
    raw = os.getenv("REPEAT_ANALYSIS_SKILLS", "")
    if raw.strip():
        return frozenset(s.strip() for s in raw.split(",") if s.strip())
    return _DEFAULT_ANALYSIS_SKILLS


def repeat_analysis_guard_enabled() -> bool:
    return os.getenv("ENABLE_REPEAT_ANALYSIS_GUARD", "0") == "1"


def is_guarded_analysis(skill: str) -> bool:
    return skill in analysis_skills()


def analysis_key(skill: str, arguments: dict) -> str:
    """skill + best-effort target. Same-file re-review → same key; different file → different key."""
    target = ""
    if isinstance(arguments, dict):
        for k in _TARGET_ARG_KEYS:
            v = arguments.get(k)
            if isinstance(v, str) and v.strip():
                target = v.strip()
                break
    return f"{skill}::{target}" if target else skill


def build_analysis_steer(skill: str, key: str) -> dict:
    """Grounded result returned in place of a redundant re-analysis."""
    target = key.split("::", 1)[1] if "::" in key else ""
    where = f" on {target}" if target else ""
    return {
        "name": "call_skill_tool",
        "response": {
            "status": "already_analyzed",
            "message": (
                f"You already ran {skill}{where} this goal and have its findings. "
                f"Do NOT re-review — make the edit now with write_file, then run the tests to "
                f"verify. If you already edited, run the tests rather than reviewing again."
            ),
        },
    }
```

- [ ] **Step 4: Run — verify pass** (7 passed)
- [ ] **Step 5: Commit** `git add services/orchestrator/repeat_analysis_guard.py tests/services/orchestrator/test_repeat_analysis_guard.py && git commit -m "feat(orchestrator): repeat-analysis guard helper (flag-gated)"`

---

## Task 2: Wire the guard into `_run_react_loop`

**Files:** Modify `services/orchestrator/coding_orchestrator.py`; Test `tests/services/orchestrator/test_repeat_analysis_guard_loop.py`

**Interfaces consumed:** the Task-1 helpers.

- [ ] **Step 1: Write the failing loop test**

Drive the ReAct loop with a fake model that emits **two identical `code-review` `call_skill_tool`
calls** on the same file, then a finish. Mirror the existing loop-test setup — find the pattern
with `grep -rn "load_skill.deduped\|already_loaded\|_run_react_loop" tests/services/orchestrator/`
and copy that test's harness (fake model + stub `skill_router` recording `execute` calls). Assert:

```python
# tests/services/orchestrator/test_repeat_analysis_guard_loop.py — assertions (adapt the harness
# from the existing _run_react_loop / load_skill-dedup test in this dir):
#
# Flag OFF (default): the stub skill_router.execute is called TWICE for the two code-review calls
#   (behavior-preserving — no short-circuit).
# Flag ON  (monkeypatch ENABLE_REPEAT_ANALYSIS_GUARD=1): execute is called ONCE; the second call
#   returns a result whose status == "already_analyzed" (the steer), and an "analysis.deduped"
#   event is emitted.
```

Concretely the two assertions are `assert stub.execute_call_count == 2` (off) and
`assert stub.execute_call_count == 1` + the steer/`analysis.deduped` present (on).

- [ ] **Step 2: Run — verify fail** (guard not wired; ON case sees execute called twice)

- [ ] **Step 3: Implement the wiring**

In `coding_orchestrator.py`:

(a) Add the import near the other guard imports (by `from .load_skill_guard import …`, ~line 20):
```python
from .repeat_analysis_guard import (
    analysis_key,
    build_analysis_steer,
    is_guarded_analysis,
    repeat_analysis_guard_enabled,
)
```

(b) Init loop-local state next to `loaded_skills: set[str] = set()` (~line 604):
```python
        seen_analysis: set[str] = set()  # repeat-analysis guard (loop-local, not checkpointed)
```

(c) Wrap the existing `call_skill_tool` body (the block at `elif name == "call_skill_tool" …`,
~`:1084-1140`) so a guarded repeat short-circuits BEFORE `skill_router.execute`. Change the
branch head + indent the existing body under an `else`:
```python
                    elif name == "call_skill_tool" and self.skill_router is not None:
                        _skill = args.get("skill", "")
                        _steer = None
                        if repeat_analysis_guard_enabled() and is_guarded_analysis(_skill):
                            _akey = analysis_key(_skill, args.get("arguments", {}))
                            if _akey in seen_analysis:
                                _steer = build_analysis_steer(_skill, _akey)
                            else:
                                seen_analysis.add(_akey)
                        if _steer is not None:
                            content = json.dumps(_steer)
                            await events.emit("analysis.deduped", skill=_skill, key=_akey)
                        else:
                            # ↓↓↓ the existing body (execute + artifact emit + verification +
                            #     edit-accounting), unchanged, indented one level under this else.
                            res = await self.skill_router.execute(
                                args.get("skill", ""),
                                args.get("tool", ""),
                                args.get("arguments", {}),
                            )
                            content = ground_tool_result(json.dumps(res), LABMATE_TOOL_RESULT_BUDGET)
                            # … (keep every existing line of the original block verbatim,
                            #     just indented under `else:`) …
```
Do NOT alter any line of the original body other than its indentation. The turn is consumed (no
`budget.refund()`) so persistent re-review still drains the budget backstop.

- [ ] **Step 4: Run — verify pass.** Then run the whole orchestrator suite to confirm no regression
(the flag is off by default, so it must be green unchanged):
`PYTHONPATH=. python -m pytest tests/services/orchestrator/ -q`

- [ ] **Step 5: Commit** (stage the two files by path)

---

## Task 3: Document the flag

**Files:** Modify `CLAUDE.md`

- [ ] **Step 1:** Add a row to the Agentic Fix Loop / harness knobs area:
```markdown
- **Repeat-analysis guard** (`repeat_analysis_guard.py`) — short-circuits a re-call of a read-only
  analysis skill (code-review/critique) on an already-analyzed target in `_run_react_loop`, steering
  to the edit instead of re-reviewing (the c2 churn root cause; see the 2026-07-03 spec). Flag
  `ENABLE_REPEAT_ANALYSIS_GUARD=0` (**OFF**), guarded set via `REPEAT_ANALYSIS_SKILLS`. Measure a
  whole-suite A/B on Q4 before defaulting on.
```
- [ ] **Step 2: Commit.**

---

## Post-implementation (live, on the pod — after all tasks)
- Whole-suite A/B on Q4, guard OFF vs ON, at n≥5:
  `FLAG=ENABLE_REPEAT_ANALYSIS_GUARD DEFAULT=0 VALUE=1 TRIALS=8 bash eval/seq_ab/run_flag_ab.sh`
  (all 6 cases — c1/c3/c4/c6 must not regress; watch c2 for a pass-rate lift or a
  faster-punt/lower-median-calls latency win).
- Judge with a cross-family model. Only flip the default to ON if the A/B shows a CI-clean c2 gain
  (or a clear latency win with no other-case regression) per the variance policy.
