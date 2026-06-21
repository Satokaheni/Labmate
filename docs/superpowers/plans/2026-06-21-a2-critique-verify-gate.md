# A2 Critique/Verify Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan.

**Goal:** After execution, automatically critique any `code` or `writing` artifact and loop back through `reflect` when its quality score is below threshold.

**Architecture:** `execute_node` records the latest artifact into `state["last_artifact"]` (`{"type": "code"|"writing"|"other", "payload": str}`). A new `verify` node runs after `execute` and before `check`: if the artifact type is `code` or `writing`, it dispatches the `critique` skill through the orchestrator's `skill_router.execute(...)` (the real Redis dispatch path), extracts a score, and sets `verified`/`critique_score`/`critique_notes`. A conditional edge sends sub-threshold artifacts to the existing `reflect` loop; everything else proceeds to `check`.

**Tech Stack:** Python, LangGraph StateGraph, the `critique` skill (dispatched via Redis Streams), pytest.

> **State-field consistency note:** This plan and `2026-06-21-a1-ambiguity-gate.md` both extend `services/orchestrator/types.py`. Use exactly these field names. A2 adds: `last_artifact`, `verified`, `critique_score`, `critique_notes`. A1 adds: `root_goal`, `assumptions`, `ambiguity`, `blocking_question`. Do not rename or collide. If A1 has already added its fields, append A2's after them.

> **Dispatch-path note:** The guide mentions `runner.call_skill_tool()`, but no such method exists on `SkillRunner`. The real path to run a skill from the orchestrator is `orch.skill_router.execute(skill_name, tool, arguments)`, which XADDs to the Redis skill-tasks stream and polls for the result (see `services/orchestrator/skill_router.py`). The `critique` skill is named `critique` and exposes a `critique` tool that returns a JSON result containing a numeric `score`.

---

### Task 1: Extend State with verification fields

**Files:**
- Modify: `services/orchestrator/types.py`
- Modify: `services/orchestrator/graph.py`
- Modify: `tests/services/orchestrator/test_types.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/orchestrator/test_types.py`:

```python
def test_state_has_verification_fields():
    """A2: State must carry last_artifact, verified, critique_score, critique_notes."""
    from services.orchestrator.types import State
    annotations = State.__annotations__
    assert "last_artifact" in annotations
    assert "verified" in annotations
    assert "critique_score" in annotations
    assert "critique_notes" in annotations
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/orchestrator/test_types.py::test_state_has_verification_fields -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

In `services/orchestrator/types.py`, add to the `State` TypedDict (after the A1 fields if present, else after `user_id: str`):

```python
    # A2 critique/verify gate
    last_artifact: dict               # {"type": "code"|"writing"|"other", "payload": str}
    verified: bool                    # True once verify has run on this artifact
    critique_score: float             # 0.0 .. 1.0 quality score from the critique skill
    critique_notes: str               # human-readable critique summary
```

In `services/orchestrator/graph.py`, add the threshold constant after the `QWEN_BASE` line (or after `AMBIGUITY_THRESHOLD` if A1 added it):

```python
# A2: artifacts scoring below this route back through reflect for revision.
CRITIQUE_THRESHOLD = float(os.getenv("CRITIQUE_THRESHOLD", "0.90"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/orchestrator/test_types.py::test_state_has_verification_fields -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/types.py services/orchestrator/graph.py tests/services/orchestrator/test_types.py
git commit -m "feat(orchestrator): add critique/verify State fields and threshold (A2)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Record `last_artifact` in execute_node

**Files:**
- Modify: `services/orchestrator/graph.py`
- Modify: `tests/services/orchestrator/test_graph.py`

**Classification:** `execute_node` aggregates per-goal `Result` summaries. After applying results, set `last_artifact` from the most recent successful result's summary. Type detection is a cheap heuristic: a summary containing fenced code or common code tokens (`def `, `class `, `import `, `function `, `=>`, `{`) is `code`; an otherwise prose summary over ~200 chars is `writing`; everything else is `other`.

- [ ] **Step 1: Write the failing test**

Append to `tests/services/orchestrator/test_graph.py`:

```python
@pytest.mark.mocked
class TestExecuteNodeArtifact:
    @pytest.mark.asyncio
    async def test_execute_node_sets_last_artifact_code(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator, Result

        mock_orch = MagicMock(spec=CodingOrchestrator)
        result = Result(id="root", summary="def add(a, b):\n    return a + b", ok=True)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        mock_async_orch.plan_and_dispatch = AsyncMock(return_value=[result])

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)
        state = _make_state()
        delta = await execute_node(state)
        assert delta["last_artifact"]["type"] == "code"
        assert "def add" in delta["last_artifact"]["payload"]

    @pytest.mark.asyncio
    async def test_execute_node_sets_last_artifact_writing(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator, Result

        prose = ("This paper investigates the effect of context length on retrieval "
                 "accuracy across several configurations. " * 4)
        mock_orch = MagicMock(spec=CodingOrchestrator)
        result = Result(id="root", summary=prose, ok=True)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        mock_async_orch.plan_and_dispatch = AsyncMock(return_value=[result])

        _, execute_node, *_ = make_nodes(mock_orch, mock_async_orch)
        state = _make_state()
        delta = await execute_node(state)
        assert delta["last_artifact"]["type"] == "writing"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/orchestrator/test_graph.py::TestExecuteNodeArtifact -v`
Expected: FAIL (`execute_node` does not set `last_artifact`)

- [ ] **Step 3: Write minimal implementation**

In `services/orchestrator/graph.py`, add a module-level helper near the top (after the threshold constants):

```python
def classify_artifact(text: str) -> str:
    """Cheap heuristic artifact classifier for the A2 verify gate."""
    if not text:
        return "other"
    code_markers = ("```", "def ", "class ", "import ", "function ", "=>", "};", "public ")
    if any(m in text for m in code_markers):
        return "code"
    if len(text.strip()) > 200:
        return "writing"
    return "other"
```

In `execute_node`, after the loop that applies `results` to the tree (the block ending where `markers[gid] = "completed"` is set) and before `return`, build and include `last_artifact`. Locate the existing `return` of `execute_node` and replace it so the delta carries the artifact. Concretely, after the results-apply loop add:

```python
        last_artifact = {"type": "other", "payload": ""}
        for r in results:
            if r.ok and r.summary:
                last_artifact = {
                    "type": classify_artifact(r.summary),
                    "payload": r.summary,
                }
```

Then ensure the returned delta includes it. The execute_node return delta is a dict like `{"goal_tree": tree, "step_markers": markers, ...}`; add the key:

```python
        delta = {"goal_tree": tree, "step_markers": markers}
        # ... existing keys such as current_goal_id / error are merged here ...
        delta["last_artifact"] = last_artifact
        delta["verified"] = False
        return delta
```

If `execute_node` currently returns its dict literal inline, refactor it to assign to a local `delta` dict first, add the two keys above, then `return delta`. Do not remove any existing keys.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/orchestrator/test_graph.py::TestExecuteNodeArtifact -v`
Expected: PASS

- [ ] **Step 5: Run the execute-node suite for regressions**

Run: `pytest tests/services/orchestrator/test_graph.py::TestExecuteNode -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/graph.py tests/services/orchestrator/test_graph.py
git commit -m "feat(orchestrator): record last_artifact in execute_node (A2)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 3: Add the `verify` node

**Files:**
- Modify: `services/orchestrator/graph.py`
- Modify: `tests/services/orchestrator/test_graph.py`

**Behavior:** If `last_artifact["type"]` is not `code`/`writing`, or no `skill_router` is wired, mark `verified=True`, `critique_score=1.0` and pass through. Otherwise dispatch `critique` via `orch.skill_router.execute("critique", "critique", {...})`, parse a float `score` from the result (defensively; default 1.0 on any failure so a critique outage never blocks the pipeline), and set the verification fields.

- [ ] **Step 1: Write the failing test**

Append to `tests/services/orchestrator/test_graph.py`:

```python
@pytest.mark.mocked
class TestVerifyNode:
    @pytest.mark.asyncio
    async def test_verify_passes_through_non_code_writing(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.skill_router = None
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        nodes = make_nodes(mock_orch, mock_async_orch)
        verify = nodes[6]  # 7th node (after A1's assess_ambiguity at index 5)

        state = _make_state(last_artifact={"type": "other", "payload": "ok"})
        delta = await verify(state)
        assert delta["verified"] is True
        assert delta["critique_score"] == 1.0

    @pytest.mark.asyncio
    async def test_verify_calls_critique_for_code(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_router = MagicMock()
        mock_router.execute = AsyncMock(return_value={
            "ok": True,
            "result": {"score": 0.42, "notes": "missing error handling"},
        })
        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.skill_router = mock_router
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        nodes = make_nodes(mock_orch, mock_async_orch)
        verify = nodes[6]

        state = _make_state(last_artifact={"type": "code", "payload": "def f(): pass"})
        delta = await verify(state)
        mock_router.execute.assert_awaited_once()
        assert delta["critique_score"] == 0.42
        assert delta["verified"] is True

    @pytest.mark.asyncio
    async def test_verify_defaults_score_when_critique_fails(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_router = MagicMock()
        mock_router.execute = AsyncMock(return_value={"ok": False, "error": "timeout"})
        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.skill_router = mock_router
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        nodes = make_nodes(mock_orch, mock_async_orch)
        verify = nodes[6]

        state = _make_state(last_artifact={"type": "code", "payload": "x=1"})
        delta = await verify(state)
        assert delta["critique_score"] == 1.0
        assert delta["verified"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/orchestrator/test_graph.py::TestVerifyNode -v`
Expected: FAIL (`make_nodes` has no `verify` node)

- [ ] **Step 3: Write minimal implementation**

In `services/orchestrator/graph.py`, inside `make_nodes`, add the node before the `return` statement:

```python
    async def verify(state: State) -> dict:
        """
        A2: critique code/writing artifacts. Dispatches the `critique` skill via
        the orchestrator's skill_router. Defaults score to 1.0 (pass) on any
        failure so a critique outage never blocks the pipeline.
        """
        artifact = state.get("last_artifact") or {"type": "other", "payload": ""}
        atype = artifact.get("type")
        router_obj = getattr(orch, "skill_router", None)
        if atype not in ("code", "writing") or router_obj is None:
            return {"verified": True, "critique_score": 1.0, "critique_notes": ""}

        score = 1.0
        notes = ""
        try:
            res = await router_obj.execute(
                "critique",
                "critique",
                {
                    "output": artifact.get("payload", ""),
                    "task": state.get("root_goal", ""),
                    "critique_type": atype,
                },
            )
            if isinstance(res, dict) and res.get("ok"):
                payload = res.get("result")
                if isinstance(payload, dict):
                    score = float(payload.get("score", 1.0))
                    notes = str(payload.get("notes", ""))
        except Exception:
            score = 1.0
            notes = ""
        return {"verified": True, "critique_score": score, "critique_notes": notes}
```

Update the `return` of `make_nodes`. If A1 has been applied it currently reads:

```python
    return plan, execute_node, check, reflect, approval, assess_ambiguity
```

Change it to:

```python
    return plan, execute_node, check, reflect, approval, assess_ambiguity, verify
```

(If A1 has NOT been applied, the base return is `return plan, execute_node, check, reflect, approval`; in that case append `, verify` and the verify tests use index `nodes[5]` instead of `nodes[6]` — but A1 should land first.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/orchestrator/test_graph.py::TestVerifyNode -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph.py tests/services/orchestrator/test_graph.py
git commit -m "feat(orchestrator): add verify node calling critique skill (A2)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 4: Wire the verify node and its routing edge

**Files:**
- Modify: `services/orchestrator/graph.py`
- Modify: `tests/services/orchestrator/test_graph.py`

**Edge logic:** `execute → verify`; then `verify_router(state)` returns `"reflect"` when `critique_score < CRITIQUE_THRESHOLD`, else `"check"`. The existing `execute → check` edge is replaced by `execute → verify → (reflect|check)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/services/orchestrator/test_graph.py`:

```python
@pytest.mark.mocked
class TestVerifyRouter:
    def test_verify_router_routes_to_reflect_when_below_threshold(self):
        from services.orchestrator.graph import verify_router
        state = _make_state(critique_score=0.5)
        assert verify_router(state) == "reflect"

    def test_verify_router_routes_to_check_when_at_threshold(self):
        from services.orchestrator.graph import verify_router
        state = _make_state(critique_score=0.95)
        assert verify_router(state) == "check"

    def test_verify_router_defaults_to_check_when_missing(self):
        from services.orchestrator.graph import verify_router
        state = _make_state()
        assert verify_router(state) == "check"


@pytest.mark.mocked
class TestBuildGraphWithVerify:
    def test_build_graph_wires_verify_node(self):
        from unittest.mock import patch
        from services.orchestrator.graph import build_graph
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator
        from langgraph.checkpoint.memory import MemorySaver

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)
        real_cp = MemorySaver()
        with patch("pymongo.MongoClient"):
            with patch("langgraph.checkpoint.mongodb.MongoDBSaver", return_value=real_cp):
                graph, _ = build_graph(mock_orch, mock_async_orch)
                assert "verify" in set(graph.nodes.keys())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/orchestrator/test_graph.py::TestVerifyRouter tests/services/orchestrator/test_graph.py::TestBuildGraphWithVerify -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

In `services/orchestrator/graph.py`, add the router after `ambiguity_router` (or after `router` if A1 not present):

```python
def verify_router(state: State) -> str:
    """
    A2: route after verify. Sub-threshold code/writing artifacts loop back
    through reflect for revision; everything else proceeds to check.
    """
    if float(state.get("critique_score", 1.0)) < CRITIQUE_THRESHOLD:
        return "reflect"
    return "check"
```

In `build_graph`, update the unpacking to capture `verify`. If A1 is applied it reads:

```python
    plan_node, execute_node, check_node, reflect_node, approval_node, assess_node = make_nodes(
        orch, async_orch
    )
```

Change to:

```python
    plan_node, execute_node, check_node, reflect_node, approval_node, assess_node, verify_node = make_nodes(
        orch, async_orch
    )
```

Register the node after the others:

```python
    b.add_node("verify", verify_node)
```

Replace the edge `b.add_edge("execute", "check")` with:

```python
    b.add_edge("execute", "verify")
    b.add_conditional_edges("verify", verify_router, ["reflect", "check"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/orchestrator/test_graph.py::TestVerifyRouter tests/services/orchestrator/test_graph.py::TestBuildGraphWithVerify -v`
Expected: PASS

- [ ] **Step 5: Run the full graph suite for regressions**

Run: `pytest tests/services/orchestrator/test_graph.py -v`
Expected: PASS (the e2e tests still finalize; verify defaults to score 1.0 → "check" because their mock orchestrators have `skill_router = None`, so the `execute → verify → check` path is transparent)

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/graph.py tests/services/orchestrator/test_graph.py
git commit -m "feat(orchestrator): wire verify gate between execute and check (A2)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```
