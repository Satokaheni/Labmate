# LangGraph-Removal Spike — Engineering Scorecard

> Decider part (ii). Behavior ties (Task 8 parity gate), so this + the
> Task 9 fault-injection resilience A/B are the decision inputs.

## LOC delta

- Lite code added: **380 LOC** (41 + 39 + 223 + 27 across 4 new modules + ~50 in-file for the approval channel + `_run_engine`).
- `graph.py` total: 1060 LOC.
- LangGraph scaffolding removable-if-adopted: **~28 LOC** (the `StateGraph` wiring, `add_node`/`add_edge`/`add_conditional_edges`, `.compile`, `interrupt`, and the two checkpointer helpers).

  **Honesty note:** graph.py's node *bodies* (assess-ambiguity, reflect, etc.) are LOGIC that lite REPRODUCES — they are not dead weight removed. The genuine removal is the graph *scaffolding* + the checkpointer SQLite layer, not all 1060 lines of graph.py.

## Droppable dependencies

Removable from `services/orchestrator/requirements.txt` once `graph.py` + its checkpointer go:

- `langgraph>=0.2`
- `langgraph-checkpoint-sqlite>=3.1`

## Ergonomics — adding one new gate (a worked comparison)

Adding a new conditional gate (e.g. a "safety check" between execute and finish):

- **graph:** define a node coroutine `async def safety(state) -> dict`, register it
  with `builder.add_node("safety", safety)`, then rewire the surrounding edges —
  add a `add_conditional_edges("execute", safety_router, {...})` plus a `safety_router`
  function returning the next-node string. Four coordinated edits across the graph
  wiring, and the control flow lives in the edge map, not in reading order.
- **lite:** add one `if`-block in `run_goal_lite` at the point it belongs. The control
  flow is the code's reading order; no node registration, no router, no edge map.

The lite path trades LangGraph's declarative graph (and its built-in checkpointer
`interrupt()` resume) for plain async control flow that is read top-to-bottom. The
cost paid back: durable suspend/resume is now hand-rolled (`lite_persistence` +
`SignalRegistry.await_approval`) instead of free from the checkpointer — which is
exactly what the Task 9 fault-injection A/B measures.
