# Deferred Tool-Search + Routing Pre-Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking. Per CLAUDE.md: **Haiku implements → Opus judges** each
> task; if React is touched, run `react-doctor` first (N/A here — all Python).

**Goal:** Cut the felt-latency of no-match routing (the validated 19s 3-sample vote) AND the fixed
prompt prefix (`tool_schemas` 52% + `skill_catalog` 37% of a ~7k prompt) — with a semantic routing
pre-gate plus a cache-safe deferred tool-search bridge, mirroring the hermes-agent + openclaw
mechanics so capability schemas/descriptions flow through the message TAIL (tool results), never
into the byte-stable prefix.

**Architecture:** Four phases, lowest-risk / highest-felt-value first.
- **Phase 0 (latency, model-independent):** a semantic pre-gate in `SkillRouter.route()` — embed
  the task, cosine-match against the pre-embedded skill catalog, and if nothing crosses a threshold,
  skip the `SELECT_ATTEMPTS` vote entirely and fall through to direct answer. Reuses the in-process
  `embed()`.
- **Phase 1 (window/VRAM):** a `find_tools`/`describe_tool`/`call_tool` bridge; the deferrable
  aux/MCP tool schemas leave the prefix, replaced by a searchable catalog surfaced in the tail.
- **Phase 2 (window, RECALL-GATED):** terse/names-only skill catalog, gated on the routing eval.
- **Phase 3 (variable cost):** smarter reads (line-range `read_file`, snippet-returning search).

**Tech Stack:** Python 3 / asyncio, litellm→llama.cpp, `services/memory/embedder.py` (BAAI/bge-
small-en-v1.5, 384-dim, Redis-cached), pytest + pytest-asyncio + pytest-bdd. No new deps.

**Research basis:** `research/llm-harness-research/token-cost-reduction-findings.md` (the completed
`/research` artifact). Its gating prerequisite is RESOLVED: `--swa-full` restored prefix-cache reuse
(240×, `cache_n=13651`), so we are in the **cache-is-reused / target** fork — optimize for
window+VRAM with **cache-safe** levers; avoid prefix-breaking ones. The research's explicit verdict:
**do the high-leverage pieces and STOP** — don't gold-plate a prefix the cache already makes cheap.

## Global Constraints

- **Cache-safety is THE invariant.** The visible `tools=` array + the system-prompt catalog region
  stay **byte-stable per goal**. Loading a capability must NEVER mutate them — everything a
  `find_tools`/`describe_tool` call surfaces goes into a `{"role":"tool", ...}` tail message.
  `PromptAssembler.canonical_prefix()` / `prefix_fingerprint()` (prompt_assembler.py:327-332) is the
  cache contract; every phase adds a test asserting the fingerprint is identical across turns with
  the feature ON.
- **Flag-gated, OFF by default → byte-identical to today when off.** `ENABLE_ROUTING_PREGATE=0`,
  `ENABLE_TOOL_SEARCH=0`, `SKILL_CATALOG_MODE=full`. No behavior change until a flag is flipped, and
  each flip is validated by the routing eval before it becomes the default.
- **Deterministic serialization** — stable JSON key order / stable tool ordering (already
  `sort_keys` in `canonical_prefix`, and client tools sorted by name in `build_tool_list`:479).
  Non-deterministic ordering silently breaks the prefix; any new partition must sort deterministically.
- **Core tools are never deferred:** `read_file`, `write_file`, `list_dir`, `search_files`,
  `run_tests`, `run_bash`, `finish`, `call_skill_tool`, `load_skill`. Bridge-tool names
  (`find_tools`, `describe_tool`, `call_tool`) are reserved.
- **Best-effort, never break the loop:** a catalog/search/embed failure falls back to prior behavior
  (advertise the tool normally / run the full vote), logged to stderr — never `print`/stdout, never
  raises into the model call.
- **Measure-first:** every phase re-runs `measure_prompt_segments` (prompt_assembler.py:335-385) for
  real before/after token numbers, and the routing eval (`eval/run_routing_eval.py`) to catch recall
  regressions. Acceptance for any recall-affecting change: **no skill's routing accuracy drops
  > 0.05** (repo standard).

## File Structure

- **Create** `services/orchestrator/routing_pregate.py` — `SkillPreGate` (embed-based
  task→catalog match; decides whether any skill plausibly matches). One responsibility: the skip
  decision. No routing logic, no I/O beyond `embed()`.
- **Create** `services/orchestrator/tool_search.py` — `ToolCatalog` (deterministic scorer over the
  deferrable tool schemas) + the 3 bridge tool schemas. One responsibility: index/search/describe
  the deferred pool. No dispatch, no prefix assembly.
- **Modify** `services/orchestrator/skill_router.py` — wire `SkillPreGate` into `route()` before
  `_confidence_check` (skill_router.py:210-246).
- **Modify** `services/orchestrator/tool_manifest.py` — `build_tool_list` partitions
  visible=core+bridge vs deferred when tool-search is on (tool_manifest.py:340-498).
- **Modify** `services/orchestrator/prompt_assembler.py` — thread the `enable_tool_search` flag into
  `build_tool_list`; add a `SKILL_CATALOG_MODE` branch to the catalog region (Phase 2).
- **Modify** `services/orchestrator/coding_orchestrator.py` — add `find_tools`/`describe_tool`/
  `call_tool` dispatch elifs in `_run_react_loop` (coding_orchestrator.py:854-1373).
- **Tests** mirror under `tests/services/orchestrator/` + a `features/*.feature` per phase.

---

## Phase 0 — Routing pre-gate (latency; model-independent; RECALL-GATED)

**Why first:** kills the validated 19s no-match routing waste, is independent of the model/window
decision, and builds the embed-match machinery Phase 1 reuses. It is recall-gated the same way
Phase 2 is: skipping routing on a task that DID have a matching skill is a recall regression, so the
threshold must be conservative and validated on the routing eval before the flag defaults on.

### Task 0.1: `SkillPreGate` — embed-based plausibility check

**Files:**
- Create: `services/orchestrator/routing_pregate.py`
- Test: `tests/services/orchestrator/test_routing_pregate.py`

**Interfaces:**
- Consumes: `services.memory.embedder.embed(texts, redis) -> list[list[float]]` (384-dim, normalized).
- Produces:
  ```python
  class SkillPreGate:
      def __init__(self, catalog: dict[str, str], *, redis=None,
                   threshold: float = PREGATE_SIM_THRESHOLD, embed_fn=embed) -> None: ...
      async def any_plausible_skill(self, task: str) -> bool: ...
  ```
  `catalog` = `{skill_name: description}` (from `skill_router.runner.catalog`). `any_plausible_skill`
  returns True if `max(cosine(task, skill_i)) >= threshold`, else False. Vectors are normalized, so
  cosine = dot product. Catalog embeddings are computed lazily once and cached on the instance.

- [ ] **Step 1 — failing test.** Assert: (a) a task semantically close to a catalog description
  returns True; (b) an off-topic task ("what is the capital of France") returns False; (c) an empty
  catalog returns False; (d) an `embed_fn` that raises makes `any_plausible_skill` return **True**
  (fail-safe: never skip routing on embed failure). Use a fake `embed_fn` returning fixed vectors so
  the test is deterministic and GPU-free.

```python
import pytest
from services.orchestrator.routing_pregate import SkillPreGate

pytestmark = pytest.mark.asyncio

def _fake_embed(vectors):
    async def _e(texts, redis=None):
        return [vectors[t] for t in texts]
    return _e

async def test_close_task_is_plausible():
    cat = {"code-review": "review code for bugs and quality issues"}
    emb = _fake_embed({
        "code-review: review code for bugs and quality issues": [1.0, 0.0],
        "please review my code for bugs": [0.98, 0.198],  # cos ~0.98
    })
    gate = SkillPreGate(cat, threshold=0.5, embed_fn=emb)
    assert await gate.any_plausible_skill("please review my code for bugs") is True

async def test_offtopic_task_is_implausible():
    cat = {"code-review": "review code for bugs and quality issues"}
    emb = _fake_embed({
        "code-review: review code for bugs and quality issues": [1.0, 0.0],
        "what is the capital of France": [0.0, 1.0],  # cos 0.0
    })
    gate = SkillPreGate(cat, threshold=0.5, embed_fn=emb)
    assert await gate.any_plausible_skill("what is the capital of France") is False

async def test_empty_catalog_is_implausible():
    gate = SkillPreGate({}, threshold=0.5, embed_fn=_fake_embed({}))
    assert await gate.any_plausible_skill("anything") is False

async def test_embed_failure_fails_safe_to_true():
    async def _boom(texts, redis=None):
        raise RuntimeError("embed down")
    gate = SkillPreGate({"x": "y"}, threshold=0.5, embed_fn=_boom)
    assert await gate.any_plausible_skill("anything") is True  # never skip on failure
```

- [ ] **Step 2 — run, verify FAIL** (`ModuleNotFoundError` / assertion).
  `python -m pytest tests/services/orchestrator/test_routing_pregate.py -v`
- [ ] **Step 3 — implement.** Lazy one-time catalog embedding; fail-safe True on any exception.

```python
"""Semantic pre-gate: skip skill routing when no skill plausibly matches the task.

The SELECT_ATTEMPTS skill-routing vote is expensive (validated ~19s on a no-match task
that falls through to direct answer anyway). This gate embeds the task once and cosine-
matches it against the pre-embedded catalog; below threshold, route() skips the vote.
FAIL-SAFE: any embed error returns True (proceed to the full vote) — never skip on doubt.
"""
from __future__ import annotations

import os

from services.memory.embedder import embed

PREGATE_SIM_THRESHOLD = float(os.getenv("PREGATE_SIM_THRESHOLD", "0.30"))


class SkillPreGate:
    def __init__(self, catalog, *, redis=None, threshold=PREGATE_SIM_THRESHOLD, embed_fn=embed):
        # catalog: {skill_name: description}. Sorted for deterministic embedding order.
        self._entries = sorted(catalog.items())
        self._redis = redis
        self._threshold = threshold
        self._embed_fn = embed_fn
        self._cat_vecs: list[list[float]] | None = None

    async def _ensure_catalog(self) -> None:
        if self._cat_vecs is not None or not self._entries:
            return
        texts = [f"{name}: {desc}" for name, desc in self._entries]
        self._cat_vecs = await self._embed_fn(texts, self._redis)

    async def any_plausible_skill(self, task: str) -> bool:
        if not self._entries:
            return False
        try:
            await self._ensure_catalog()
            (task_vec,) = await self._embed_fn([task], self._redis)
            best = max(_dot(task_vec, v) for v in (self._cat_vecs or []))
        except Exception:  # noqa: BLE001 — fail-safe: proceed to the full vote
            return True
        return best >= self._threshold


def _dot(a, b) -> float:
    # embeddings are L2-normalized, so dot product == cosine similarity
    return sum(x * y for x, y in zip(a, b))
```

- [ ] **Step 4 — run, verify PASS.**
- [ ] **Step 5 — commit.** `git add services/orchestrator/routing_pregate.py tests/services/orchestrator/test_routing_pregate.py && git commit -m "feat(routing): SkillPreGate — embed-based plausibility check for the routing pre-gate"`

### Task 0.2: Wire the pre-gate into `route()` (flag-gated, fail-safe)

**Files:**
- Modify: `services/orchestrator/skill_router.py` (`route()` :210-246; add module-level flag + a
  lazily-built `self._pregate`)
- Test: `tests/services/orchestrator/test_route_pregate_wiring.py`

**Interfaces:**
- Consumes: `SkillPreGate.any_plausible_skill`. Adds `ENABLE_ROUTING_PREGATE = os.getenv(...)=="1"`
  (default **0/off**) near the other module flags.
- Produces: unchanged `route()` signature; when the flag is on AND `any_plausible_skill` is False,
  `route()` returns `RouteResult(skills=[], needs_clarification=False, sub_intents=[task])`
  **without** calling `_confidence_check`.

- [ ] **Step 1 — failing test.** With the flag on: (a) a pre-gate that returns False makes `route()`
  return an empty-skills result AND `_confidence_check` is never awaited (patch it with an
  `AssertionError` side-effect); (b) a pre-gate returning True still runs the normal vote; (c) with
  the flag OFF, the pre-gate is never consulted (byte-identical to today). Drive the real `route()`.

```python
import pytest
from unittest.mock import AsyncMock, patch
from services.orchestrator import skill_router as SR

pytestmark = pytest.mark.asyncio

def _router():
    from services.orchestrator.skill_router import SkillRouter
    from unittest.mock import MagicMock
    runner = MagicMock()
    runner.catalog = {"code-review": "review code"}
    return SkillRouter(runner=runner, redis=AsyncMock(), gemma_api_base="http://x/v1")

async def test_pregate_skips_vote_when_implausible(monkeypatch):
    monkeypatch.setattr(SR, "ENABLE_ROUTING_PREGATE", True)
    router = _router()
    router._pregate = AsyncMock()
    router._pregate.any_plausible_skill.return_value = False
    with patch.object(router, "_confidence_check",
                      side_effect=AssertionError("vote must not run")):
        res = await router.route("what is the capital of France")
    assert res.skills == []

async def test_pregate_allows_vote_when_plausible(monkeypatch):
    monkeypatch.setattr(SR, "ENABLE_ROUTING_PREGATE", True)
    router = _router()
    router._pregate = AsyncMock()
    router._pregate.any_plausible_skill.return_value = True
    with patch.object(router, "_confidence_check",
                      new=AsyncMock(return_value=("code-review", 1.0))):
        res = await router.route("review my code")
    assert res.skills == ["code-review"]

async def test_flag_off_never_consults_pregate(monkeypatch):
    monkeypatch.setattr(SR, "ENABLE_ROUTING_PREGATE", False)
    router = _router()
    router._pregate = AsyncMock(side_effect=AssertionError("pregate must not run"))
    with patch.object(router, "_confidence_check",
                      new=AsyncMock(return_value=(None, 0.0))):
        res = await router.route("anything")
    assert res.skills == []
```

- [ ] **Step 2 — run, verify FAIL.**
- [ ] **Step 3 — implement.** Add the flag; build `self._pregate` lazily from `self._runner.catalog`
  (guard: only when `ENABLE_ROUTING_PREGATE` and the catalog is non-empty); insert the check as the
  first lines of `route()` before `sub_intents = [task]`'s vote:

```python
# module scope, near SELECT_ATTEMPTS / CONFIDENCE_THRESHOLD
ENABLE_ROUTING_PREGATE = os.getenv("ENABLE_ROUTING_PREGATE", "0") == "1"

# inside route(), at the top:
if ENABLE_ROUTING_PREGATE:
    if self._pregate is None:
        from services.orchestrator.routing_pregate import SkillPreGate
        self._pregate = SkillPreGate(dict(self._runner.catalog), redis=self._redis)
    if not await self._pregate.any_plausible_skill(task):
        _log.info("route() pre-gate: no plausible skill -> skip vote, direct answer")
        return RouteResult(skills=[], needs_clarification=False, sub_intents=[task])
```
  Initialize `self._pregate = None` in `__init__`.

- [ ] **Step 4 — run, verify PASS.** Then full router suite:
  `python -m pytest tests/services/orchestrator/test_skill_router*.py tests/services/orchestrator/test_route_pregate_wiring.py -q`
- [ ] **Step 5 — commit.**

### Task 0.3: Pre-gate recall eval (BLOCKING gate before default-on)

**Why this replaces a `run_routing_eval` sweep:** `eval/run_routing_eval.py` is a faithful COPY
of the routing loop (its `route_one` SEAM), not the real `SkillRouter` — it never constructs a
router, so `ENABLE_ROUTING_PREGATE` has ZERO effect on it. A flag-on/off sweep there measures
nothing about the pre-gate. Instead, exercise the REAL `SkillPreGate` directly and measure the only
recall risk that matters: a **false-skip** — the pre-gate returning False (skip routing) for a case
that has an expected skill.

**Files:**
- Modify: `services/orchestrator/routing_pregate.py` — add `async def max_similarity(self, task: str)
  -> float` and refactor `any_plausible_skill` to use it (single source for the cosine logic).
- Create: `eval/pregate_recall_eval.py` — sweep thresholds over the eval cases, report false-skip /
  correct-skip rates + per-skill false-skip breakdown, recommend a threshold.
- Test: extend `tests/services/orchestrator/test_routing_pregate.py` (max_similarity); create
  `tests/eval/test_pregate_recall_eval.py` (the pure aggregation core).

**Interfaces:**
- `SkillPreGate.max_similarity(task) -> float`: best cosine over the catalog. Empty catalog →
  `float("-inf")` (nothing matches → `any_plausible_skill` False for any threshold). On embed error →
  `float("inf")` (FAIL-SAFE → `any_plausible_skill` True). `any_plausible_skill` becomes exactly
  `await self.max_similarity(task) >= self._threshold`. The four existing pre-gate tests MUST stay
  green (behavior-preserving refactor).
- Pure core: `summarize_skips(rows: list[dict], thresholds: list[float]) -> list[dict]`, where each
  row is `{"expected": str, "max_sim": float}` (`expected == "none"` = no skill). Returns, per
  threshold: `{threshold, false_skip_rate, correct_skip_rate, per_skill_false_skip: {skill: rate},
  n_skill, n_none}`. false_skip = expected-skill case with `max_sim < threshold`; correct_skip =
  `none` case with `max_sim < threshold`.

- [ ] **Step 1 — failing tests.** (a) `max_similarity` via fake embed: close task → high sim,
  off-topic → low, empty catalog → `-inf`, embed error → `inf`; and the four ORIGINAL
  `any_plausible_skill` tests still pass unchanged. (b) `summarize_skips` on synthetic rows: a
  false-skip and a correct-skip are each counted into the right bucket at the right threshold;
  per-skill breakdown groups false-skips by `expected`; empty input is safe.
- [ ] **Step 2 — run, verify FAIL.**
- [ ] **Step 3 — implement** the refactor + `summarize_skips` + the live harness. The harness (run on
  the host, not unit-tested): load `eval/routing_eval.jsonl` + the catalog via `SkillRunner`, build a
  real `SkillPreGate`, compute `max_similarity` per case ONCE (plus a built-in list of known no-match
  conversational probes — "what is the traveling salesman problem", "what is the capital of France" —
  to exercise the correct-skip win), then `summarize_skips` over the threshold sweep. Print a table
  and the **recommended threshold = the highest at which every per-skill false-skip rate ≤ 0.05**.
- [ ] **Step 4 — run, verify PASS** (unit tests). ruff clean. Commit.
- [ ] **Step 5 — HOST run (hand-off; BLOCKING gate).**
  `python eval/pregate_recall_eval.py --eval eval/routing_eval.jsonl --skills-dir services/skills --thresholds 0.20,0.25,0.30,0.35,0.40`.
  **Acceptance:** adopt the recommended threshold (highest with every per-skill false-skip ≤ 0.05).
  Only then does `PREGATE_SIM_THRESHOLD` default to it and `ENABLE_ROUTING_PREGATE` flip to 1
  (separate commit). Then a live latency confirm: push "what is the traveling salesman problem" with
  the gate on → `route() pre-gate: no plausible skill` in the log, pre-answer latency toward the ~2s
  assess-only floor, not 19s.

> **Optional (gated) Task 0.4 — SELECT_ATTEMPTS serialization.** With the pre-gate absorbing the
> no-match case, the 3-into-2-slots tail matters less. If still worth it after 0.3, evaluate
> reducing `SELECT_ATTEMPTS` to match slots OR a fire-1-then-confirm inversion — but that changes the
> confidence denominator, so it is **routing-eval-gated** identically. Do NOT ship without the eval.

---

## Phase 1 — Deferred tool-search bridge (window/VRAM; no routing-recall risk)

Defer the non-core tools (client MCP/skill tools + `code_semantic_search` + `memory_search`) behind
a `find_tools`/`describe_tool`/`call_tool` bridge; keep core tools and the full skill catalog as-is.
Proves the cache-safe machinery on the low-risk pool.

**The `call_tool` decision is settled:** local models won't emit a tool_call for a name absent from
`tools=` (hermes + openclaw both ship a call bridge), so `call_tool(name, args)` is required — mirror
hermes `tools/tool_search.py:680-710`.

### Task 1.1: `ToolCatalog` + bridge schemas

**Files:**
- Create: `services/orchestrator/tool_search.py`
- Test: `tests/services/orchestrator/test_tool_search.py`

**Interfaces:**
- Produces:
  ```python
  class ToolCatalog:
      def __init__(self, deferrable: list[dict]) -> None: ...   # list of OpenAI tool schemas
      def search(self, query: str, k: int = 5) -> list[dict]: ...  # [{name, description}]
      def describe(self, name: str) -> dict | None: ...            # full schema or None
  def find_tools_schema() -> dict
  def describe_tool_schema() -> dict
  def call_tool_schema() -> dict
  ```
  Scorer: deterministic tokenized-overlap (lowercase, split on non-alnum) over name + description +
  top-level param names — hermes `tool_search.py:347-418` shape. Ties broken by name (stable).

- [ ] **Step 1 — failing test.** Assert: `search` ranks a name/description match above an unrelated
  tool and returns `[{name, description}]` (no params — terse); `describe` returns the full schema
  for a known name and `None` for an unknown one; the three `*_schema()` fns return valid OpenAI
  function schemas with the reserved names; scorer is deterministic across two calls (stable order).
- [ ] **Step 2 — run, verify FAIL.**
- [ ] **Step 3 — implement** `ToolCatalog` (index `{name: schema}` + a token set per tool) and the
  three terse bridge schemas (`find_tools(query, k?)`, `describe_tool(name)`,
  `call_tool(name, arguments)`), with a module docstring noting "every byte in these schemas is paid
  every turn — keep terse."
- [ ] **Step 4 — run, verify PASS.**
- [ ] **Step 5 — commit.**

### Task 1.2: `build_tool_list` partition (visible=core+bridge vs deferred)

**Files:**
- Modify: `services/orchestrator/tool_manifest.py` (`build_tool_list` :340-498; add
  `enable_tool_search: bool = False` kwarg)
- Modify: `services/orchestrator/prompt_assembler.py` (`__init__` :313-319 — pass the flag through)
- Test: `tests/services/orchestrator/test_build_tool_list_partition.py`

**Interfaces:**
- Consumes: `find_tools_schema`/`describe_tool_schema`/`call_tool_schema` from `tool_search`.
- Produces: when `enable_tool_search` is True, the returned list = **core builtins + load_skill +
  call_skill_tool + finish + [find_tools, describe_tool, call_tool]** — with the client MCP/skill
  tools, `code_semantic_search`, and `memory_search` **removed** from the visible list (they become
  the deferred pool). When False, the list is byte-identical to today. The deferred pool is exposed
  via a new return or a side-channel the assembler stores (e.g. `build_tool_list` returns
  `(visible, deferred)` when the flag is on — keep the single-list return when off for compat, or
  always return a tuple and update the one call site in `PromptAssembler.__init__`).

- [ ] **Step 1 — failing test.** With flag on: (a) `find_tools`/`describe_tool`/`call_tool` present
  in visible; (b) `mcp__*`, `code_semantic_search`, `memory_search` absent from visible; (c) all core
  tools present; (d) the deferred pool contains exactly the removed tools; (e) with flag off, output
  equals the current `build_tool_list` output (snapshot-compare). Deterministic order asserted.
- [ ] **Step 2 — run, verify FAIL.**
- [ ] **Step 3 — implement** the partition; keep the deterministic sort; guard the whole branch on
  the flag so the off-path is untouched.
- [ ] **Step 4 — run, verify PASS** + full `tool_manifest`/`prompt_assembler` suites.
- [ ] **Step 5 — commit.**

### Task 1.3: Prefix-cache stability test (the invariant)

**Files:** Test: `tests/services/orchestrator/test_tool_search_prefix_stable.py`

- [ ] **Step 1 — test.** Build two `PromptAssembler`s with `enable_tool_search=True` and identical
  inputs across two "turns"; assert `prefix_fingerprint()` is identical, and that it **differs** from
  the flag-off assembler (proving the deferral actually changed the prefix). Also assert the visible
  tool count dropped by exactly `len(deferred)` minus 3 bridge tools.
- [ ] **Step 2 — run, verify PASS** (implementation already exists from 1.2; this locks the invariant).
- [ ] **Step 3 — commit.**

### Task 1.4: Bridge dispatch in `_run_react_loop`

**Files:**
- Modify: `services/orchestrator/coding_orchestrator.py` (`_run_react_loop` dispatch chain
  :854-1373; add three elifs before the final `else` at :1346; construct a `ToolCatalog` from the
  deferred pool once per goal)
- Test: `tests/services/orchestrator/test_react_bridge_dispatch.py`

**Interfaces:**
- `find_tools(query, k)` → tool result = JSON list of `{name, description}` from
  `ToolCatalog.search`, passed through `ground_tool_result(..., LABMATE_TOOL_RESULT_BUDGET)`.
- `describe_tool(name)` → tool result = the full schema JSON (or an enumerated "unknown tool" error).
- `call_tool(name, arguments)` → **re-enters the existing dispatch by the real name** (so a described
  `mcp__codegraph__*` / `code_semantic_search` / `memory_search` executes exactly as it does today);
  result appended to messages via the standard `{"role":"tool", ...}` block (:1366-1373). Unknown /
  non-deferred names return the enumerated error, never raise.

- [ ] **Step 1 — failing test.** Drive `_run_react_loop` (or the dispatch helper) with a fake model
  emitting: `find_tools("semantic code search")` → result contains `code_semantic_search`;
  `describe_tool("code_semantic_search")` → result is its full schema; `call_tool("code_semantic_search",
  {"query":"x"})` → routes to the existing `codegraph_mcp.call_tool` path (assert it was called with
  the real name); `call_tool("bogus", {})` → enumerated error, loop continues.
- [ ] **Step 2 — run, verify FAIL.**
- [ ] **Step 3 — implement** the three elifs; factor the real-name dispatch so `call_tool` and the
  native path share one code path (avoid divergence).
- [ ] **Step 4 — run, verify PASS.**
- [ ] **Step 5 — commit.**

### Task 1.5: Measure + live smoke (acceptance)

- [ ] `measure_prompt_segments` on a live turn with `ENABLE_TOOL_SEARCH=1` — assert `tool_schemas`
  drops by the deferred pool; `prefix_fingerprint` stable across turns.
- [ ] Live skill-execution smoke (CLAUDE.md §11) + the exec-seam live tests (§10) green — deferred
  tools still callable end-to-end via `call_tool`.
- [ ] Routing eval unchanged (Phase 1 doesn't touch the skill catalog) — sanity only.
- [ ] Commit a short `eval/reports/` note with the token delta.

---

## Phase 2 — Skill catalog terse/names-only (window; RECALL-GATED)

The 2.6k skill menu is the biggest single deferrable, but it's the routing signal — deferring it
trades tokens for recall risk (the "99% paradox"). **Shape branches on the routing eval, so this
phase is specified at task level; the exact variant is chosen from eval results, not pre-committed.**

**Files:** Modify `prompt_assembler.py` (catalog region :257-269) + `skill_runner.catalog_prompt` to
honor `SKILL_CATALOG_MODE = full | terse | names` (default `full`). `terse` = name + ≤10-word line;
`names` = names only, full description moved behind a `describe_skill` tail result.

- [ ] **Task 2.1** — `catalog_prompt(mode=...)` with `terse`/`names` renderers + unit tests (assert
  byte-stable output per mode; assert `full` unchanged).
- [ ] **Task 2.2** — thread `SKILL_CATALOG_MODE` through `PromptAssembler`; prefix-stability test per
  mode.
- [ ] **Task 2.3 (BLOCKING GATE)** — routing eval across all three modes. **Acceptance: no skill
  drops > 0.05.** Ship the *tersest* mode that holds; if `names` regresses, fall back to `terse`; if
  `terse` regresses, keep `full` and stop (Phase 2 is a no-go — that's a legitimate outcome, record
  it). Only the winning mode's flag flips to default.
- [ ] **Task 2.4 (only if `names` wins)** — a `describe_skill(name)` tail bridge so the model can
  fetch a full description on demand (mirror `describe_tool`).

---

## Phase 3 — Smarter reads (variable cost; cache-neutral; do regardless of the window verdict)

The research flags `tool_results` as the variable cost that actually spikes the window on real work
(the original 14k full-file-read case). Cache-neutral (tail-only), so it's safe independent of the
prefix work.

**Files:** Modify `CANONICAL_BUILTIN_SCHEMAS` (tool_manifest.py:21-122) for `read_file` +
`search_files`; the client-side local-tool handler must honor the new params (coordinate with the
local-execution layer — a client that ignores them must still return the whole file, i.e. additive
and backward-compatible).

- [ ] **Task 3.1** — `read_file` schema gains optional `offset`/`limit` (line range); client honors
  them; unit test the schema + a contract test that an old client ignoring them still works.
- [ ] **Task 3.2** — `search_files` returns a snippet (matched line ± context) instead of whole
  files; test the snippet shape and the token reduction on a fixture.
- [ ] **Task 3.3** — measure on a read-heavy fixture task; record the `tool_results` reduction.

> **Code-mode / programmatic tool calling** (results stay in a sandbox, only a summary enters
> context — the research's biggest variable-cost win) is noted as a **future stretch**, NOT in scope
> here — it's a larger surface and the research says do the high-leverage pieces and STOP.

---

## Acceptance (whole plan)

- `measure_prompt_segments` shows a real `tool_schemas` (Phase 1) and, if Phase 2 ships,
  `skill_catalog` reduction on a live turn — with `prefix_fingerprint` stable across turns (cache
  intact). Report actual before/after numbers.
- Routing eval: no skill drops > 0.05 for every recall-affecting flip (Phase 0 gate, Phase 2 gate).
- Live skill-execution + exec-seam smoke (CLAUDE.md §10–§12) green — deferred tools/skills callable.
- Phase 0 live: no-match pre-answer latency drops toward the assess-only floor.
- **Bounded/honest stop:** since the cache is reused, Phase 1/2 buy window+VRAM (not much latency);
  stop after the measured reduction plateaus. Phase 0 is the latency win; Phase 3 is the real-work
  window win. Don't chase the last few hundred prefix tokens.

## Sequencing notes

- **Phase 0 is independent** of the 12B/31B/window decision — ship it regardless (it's felt latency).
- **Phase 1 ships regardless** (safe). **Phase 2 is gated** on its eval and may legitimately be a
  no-go. **Phase 3 is independent** and worthwhile on any host/model.
- Each phase flips its own flag to default only after its gate passes — until then everything is
  OFF-by-default and byte-identical to today.
