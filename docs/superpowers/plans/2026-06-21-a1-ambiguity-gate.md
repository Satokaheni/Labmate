# A1 Ambiguity Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan.

**Goal:** Triage every task for ambiguity before planning, and route critically-underspecified tasks to the human-approval gate first.

**Architecture:** A new `assess_ambiguity` node runs at graph entry (`START → assess_ambiguity → plan`). It asks Gemma to list the assumptions an agent must make and rate ambiguity 0.0–1.0. The result is written into State (`assumptions`, `ambiguity`, `blocking_question`, `root_goal`). A conditional edge routes to `approval` when `ambiguity >= AMBIGUITY_THRESHOLD`, otherwise to `plan`.

**Tech Stack:** Python, LangGraph StateGraph, litellm (Gemma 4 31B), pytest.

> **State-field consistency note:** This plan and `2026-06-21-a2-critique-verify-gate.md` both extend `services/orchestrator/types.py`. Use exactly these field names. A1 adds: `root_goal`, `assumptions`, `ambiguity`, `blocking_question`. A2 adds: `last_artifact`, `verified`, `critique_score`, `critique_notes`. Do not rename or collide.

---

### Task 1: Extend State and add the ambiguity threshold + root_goal seeding

**Files:**
- Modify: `services/orchestrator/types.py`
- Modify: `services/orchestrator/graph.py`
- Modify: `services/orchestrator/coding_orchestrator.py`
- Modify: `tests/services/orchestrator/test_types.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/orchestrator/test_types.py`:

```python
def test_state_has_ambiguity_fields():
    """A1: State must carry root_goal, assumptions, ambiguity, blocking_question."""
    from services.orchestrator.types import State
    annotations = State.__annotations__
    assert "root_goal" in annotations
    assert "assumptions" in annotations
    assert "ambiguity" in annotations
    assert "blocking_question" in annotations
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/orchestrator/test_types.py::test_state_has_ambiguity_fields -v`
Expected: FAIL (fields not present)

- [ ] **Step 3: Write minimal implementation**

In `services/orchestrator/types.py`, add fields to the `State` TypedDict (it is `total=False`, so adding fields is safe). Insert after the `user_id: str` line:

```python
    # A1 ambiguity gate
    root_goal: str                    # the original incoming task text
    assumptions: list[str]            # assumptions an agent must make to proceed
    ambiguity: float                  # 0.0 fully specified .. 1.0 critically underspecified
    blocking_question: str            # the single question to ask the user, if any
```

In `services/orchestrator/graph.py`, add the threshold constant after the `QWEN_BASE` line (module-level constants live here, matching `SELECT_ATTEMPTS` in `skill_router.py`):

```python
# A1: tasks at or above this ambiguity score route to the approval gate before planning.
AMBIGUITY_THRESHOLD = float(os.getenv("AMBIGUITY_THRESHOLD", "0.6"))
```

In `services/orchestrator/coding_orchestrator.py`, seed `root_goal` in `run_task`'s `initial` state dict. Add after the `"user_id": user_id,` line in the `initial: State = {...}` literal:

```python
            "root_goal": task,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/orchestrator/test_types.py::test_state_has_ambiguity_fields -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/types.py services/orchestrator/graph.py services/orchestrator/coding_orchestrator.py tests/services/orchestrator/test_types.py
git commit -m "feat(orchestrator): add ambiguity State fields and threshold (A1)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Add the `assess_ambiguity` node

**Files:**
- Modify: `services/orchestrator/graph.py`
- Modify: `tests/services/orchestrator/test_graph.py`

**Note on JSON parsing:** `CodingOrchestrator` has no `complete_json` method — only `architect(prompt, thinking_budget)`. The node calls `architect` and parses JSON defensively (stripping code fences), defaulting to `ambiguity=0.0` on any parse failure so a malformed model reply never blocks the pipeline.

- [ ] **Step 1: Write the failing test**

Append to `tests/services/orchestrator/test_graph.py`:

```python
@pytest.mark.mocked
class TestAssessAmbiguityNode:
    @pytest.mark.asyncio
    async def test_assess_ambiguity_parses_json(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value=(
            '{"assumptions": ["uses python"], "ambiguity": 0.8, '
            '"blocking_question": "which framework?"}'
        ))
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        nodes = make_nodes(mock_orch, mock_async_orch)
        assess = nodes[5]  # 6th node returned by make_nodes

        state = _make_state(root_goal="build a thing")
        delta = await assess(state)
        assert delta["ambiguity"] == 0.8
        assert delta["assumptions"] == ["uses python"]
        assert delta["blocking_question"] == "which framework?"

    @pytest.mark.asyncio
    async def test_assess_ambiguity_defaults_zero_on_bad_json(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value="not json at all")
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        nodes = make_nodes(mock_orch, mock_async_orch)
        assess = nodes[5]

        state = _make_state(root_goal="anything")
        delta = await assess(state)
        assert delta["ambiguity"] == 0.0
        assert delta["assumptions"] == []

    @pytest.mark.asyncio
    async def test_assess_ambiguity_strips_code_fences(self):
        from services.orchestrator.graph import make_nodes
        from services.orchestrator.coding_orchestrator import CodingOrchestrator, AsyncOrchestrator

        mock_orch = MagicMock(spec=CodingOrchestrator)
        mock_orch.architect = AsyncMock(return_value=(
            '```json\n{"assumptions": [], "ambiguity": 0.3, "blocking_question": ""}\n```'
        ))
        mock_async_orch = MagicMock(spec=AsyncOrchestrator)

        nodes = make_nodes(mock_orch, mock_async_orch)
        assess = nodes[5]

        state = _make_state(root_goal="x")
        delta = await assess(state)
        assert delta["ambiguity"] == 0.3
```

Also update `_make_state` at the top of `test_graph.py` so `root_goal` is present by default. Change the `base` dict to include it:

```python
    base = {
        "session_id": "test-001",
        "goal_tree": tree,
        "current_goal_id": "root",
        "step_markers": {},
        "messages": [],
        "error": None,
        "root_goal": "top-level task",
    }
```

**Backward-compat note:** Existing tests in `test_graph.py` use fixed-arity unpacking of `make_nodes` — `_, _, check_node, _, _ = make_nodes(...)` (5 targets) and `_, _, _, reflect_node, _ = make_nodes(...)` (5 targets). Returning a 6th element breaks these with `ValueError: too many values to unpack`. The `plan_node, *_ = make_nodes(...)` and `_, execute_node, *_ = make_nodes(...)` forms are starred and stay valid. In this step you MUST also update every fixed-arity unpacking to add a trailing `, _` (or switch to a starred form). Do a search:

```bash
grep -n "= make_nodes(" tests/services/orchestrator/test_graph.py
```

For each `_, _, check_node, _, _ = make_nodes(mock_orch, mock_async_orch)` change it to:

```python
        _, _, check_node, _, _, _ = make_nodes(mock_orch, mock_async_orch)
```

For each `_, _, _, reflect_node, _ = make_nodes(mock_orch, mock_async_orch)` change it to:

```python
        _, _, _, reflect_node, _, _ = make_nodes(mock_orch, mock_async_orch)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/orchestrator/test_graph.py::TestAssessAmbiguityNode -v`
Expected: FAIL (`make_nodes` returns only 5 nodes; no `assess_ambiguity`)

- [ ] **Step 3: Write minimal implementation**

In `services/orchestrator/graph.py`, inside `make_nodes`, add the node before the `return` statement. Place it just before `return plan, execute_node, check, reflect, approval`:

```python
    async def assess_ambiguity(state: State) -> dict:
        """
        A1: triage the task before planning. Ask Gemma to enumerate the
        assumptions an agent must make and rate overall ambiguity 0.0-1.0.
        Parses JSON defensively; defaults to ambiguity=0.0 on any failure so a
        malformed reply never blocks the pipeline.
        """
        import json

        goal = state.get("root_goal") or state["goal_tree"][state["current_goal_id"]]["description"]
        prompt = (
            "You are triaging a task before an autonomous agent executes it.\n"
            f"TASK: {goal}\n\n"
            "List the assumptions an agent must make to act on this as written. "
            "Then rate overall ambiguity from 0.0 (fully specified) to 1.0 (critically "
            "underspecified). Respond as JSON: "
            '{"assumptions": ["..."], "ambiguity": 0.0, "blocking_question": "" }'
        )
        raw = await orch.architect(prompt, thinking_budget=1024)
        text = (raw or "").strip()
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        try:
            out = json.loads(text.strip())
            if not isinstance(out, dict):
                raise ValueError("not an object")
        except (json.JSONDecodeError, ValueError):
            out = {}
        try:
            ambiguity = float(out.get("ambiguity", 0.0))
        except (TypeError, ValueError):
            ambiguity = 0.0
        return {
            "root_goal": goal,
            "assumptions": out.get("assumptions", []) or [],
            "ambiguity": ambiguity,
            "blocking_question": out.get("blocking_question", "") or "",
        }
```

Then change the return line to include the new node:

```python
    return plan, execute_node, check, reflect, approval, assess_ambiguity
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/orchestrator/test_graph.py::TestAssessAmbiguityNode -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/orchestrator/graph.py tests/services/orchestrator/test_graph.py
git commit -m "feat(orchestrator): add assess_ambiguity node (A1)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 3: Wire the node and the ambiguity routing edge into the graph

**Files:**
- Modify: `services/orchestrator/graph.py`
- Modify: `tests/services/orchestrator/test_graph.py`

**Edge logic:** `START → assess_ambiguity`; then a conditional edge `ambiguity_router(state)` returns `"approval"` when `state["ambiguity"] >= AMBIGUITY_THRESHOLD`, else `"plan"`. The existing `START → plan` edge is removed. Existing nodes keep all current callers (the order-based unpacking in `make_nodes` is preserved because the new node is appended last).

- [ ] **Step 1: Write the failing test**

Append to `tests/services/orchestrator/test_graph.py`:

```python
@pytest.mark.mocked
class TestAmbiguityRouter:
    def test_ambiguity_router_routes_to_approval_when_high(self):
        from services.orchestrator.graph import ambiguity_router
        state = _make_state(ambiguity=0.7)
        assert ambiguity_router(state) == "approval"

    def test_ambiguity_router_routes_to_plan_when_low(self):
        from services.orchestrator.graph import ambiguity_router
        state = _make_state(ambiguity=0.2)
        assert ambiguity_router(state) == "plan"

    def test_ambiguity_router_defaults_to_plan_when_missing(self):
        from services.orchestrator.graph import ambiguity_router
        state = _make_state()
        assert ambiguity_router(state) == "plan"


@pytest.mark.mocked
class TestBuildGraphWithAmbiguity:
    def test_build_graph_wires_assess_ambiguity_node(self):
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
                assert "assess_ambiguity" in set(graph.nodes.keys())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/orchestrator/test_graph.py::TestAmbiguityRouter tests/services/orchestrator/test_graph.py::TestBuildGraphWithAmbiguity -v`
Expected: FAIL (`ambiguity_router` undefined; node not wired)

- [ ] **Step 3: Write minimal implementation**

In `services/orchestrator/graph.py`, add the router function after the existing `router(state)` function:

```python
def ambiguity_router(state: State) -> str:
    """
    A1: route after assess_ambiguity. Tasks at or above AMBIGUITY_THRESHOLD go
    to the human-approval gate before planning; everything else plans directly.
    """
    if float(state.get("ambiguity", 0.0)) >= AMBIGUITY_THRESHOLD:
        return "approval"
    return "plan"
```

In `build_graph`, update the unpacking and wiring. Replace:

```python
    plan_node, execute_node, check_node, reflect_node, approval_node = make_nodes(
        orch, async_orch
    )
```

with:

```python
    plan_node, execute_node, check_node, reflect_node, approval_node, assess_node = make_nodes(
        orch, async_orch
    )
```

Add the node registration after `b.add_node("approval", approval_node)`:

```python
    b.add_node("assess_ambiguity", assess_node)
```

Replace the entry edge `b.add_edge(START, "plan")` with:

```python
    b.add_edge(START, "assess_ambiguity")
    b.add_conditional_edges("assess_ambiguity", ambiguity_router, ["approval", "plan"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/orchestrator/test_graph.py::TestAmbiguityRouter tests/services/orchestrator/test_graph.py::TestBuildGraphWithAmbiguity -v`
Expected: PASS

- [ ] **Step 5: Run the full graph suite to confirm no regressions**

Run: `pytest tests/services/orchestrator/test_graph.py -v`
Expected: PASS (existing `make_nodes` callers using `plan_node, *_` and `_, _, check_node, _, _` still work because the new node is appended last)

- [ ] **Step 6: Commit**

```bash
git add services/orchestrator/graph.py tests/services/orchestrator/test_graph.py
git commit -m "feat(orchestrator): wire ambiguity gate into graph entry (A1)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```
