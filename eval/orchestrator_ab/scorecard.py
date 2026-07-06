"""Engineering scorecard for the LangGraph-removal spike (decider part ii).

Behavior ties (Task 8 parity gate), so the decision rests on resilience
(Task 9) + this scorecard: LOC delta, droppable deps, ergonomics.
Pure: reads source files, computes counts, renders SCORECARD.md. No
orchestrator/GPU imports.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # eval/orchestrator_ab/ -> repo root

LITE_MODULES = [
    "services/orchestrator/lite_state.py",
    "services/orchestrator/lite_persistence.py",
    "services/orchestrator/lite_orchestrator.py",
    "services/orchestrator/lite_approval.py",
]
GRAPH_PY = "services/orchestrator/graph.py"
ORCH_REQS = "services/orchestrator/requirements.txt"

# In-file lite additions that are not whole modules (measured, hand-maintained:
# re-derive via `git show a5572d5 -- services/orchestrator/inproc_bus.py` for the
# approval channel, and the `_run_engine` method span in main.py, if either changes).
INFILE_LITE_LOC = 75  # ~29 approval channel on inproc_bus.py + ~46 _run_engine in main.py

_SCAFFOLD_PATTERNS = [
    r"^\s*from langgraph",
    r"^\s*import langgraph",
    r"StateGraph\(",
    r"\.add_node\(",
    r"\.add_edge\(",
    r"\.add_conditional_edges\(",
    r"\.compile\(",
    r"interrupt\(",
    r"def _make_async_sqlite_checkpointer",
    r"def _make_sqlite_checkpointer",
]


def count_lines(rel: str, root: Path = REPO) -> int:
    p = root / rel
    return len(p.read_text().splitlines()) if p.exists() else 0


def lite_loc(root: Path = REPO) -> int:
    return sum(count_lines(m, root) for m in LITE_MODULES) + INFILE_LITE_LOC


def scaffold_loc(graph_rel: str = GRAPH_PY, root: Path = REPO) -> int:
    """Count LangGraph-scaffolding lines in graph.py (removable-if-adopted)."""
    p = root / graph_rel
    if not p.exists():
        return 0
    pats = [re.compile(pat) for pat in _SCAFFOLD_PATTERNS]
    return sum(1 for line in p.read_text().splitlines() if any(pat.search(line) for pat in pats))


def droppable_deps(reqs_rel: str = ORCH_REQS, root: Path = REPO) -> list[str]:
    p = root / reqs_rel
    if not p.exists():
        return []
    return [ln.strip() for ln in p.read_text().splitlines() if "langgraph" in ln.lower()]


def build_scorecard(root: Path = REPO) -> dict:
    return {
        "lite_loc_added": lite_loc(root),
        "graph_py_total": count_lines(GRAPH_PY, root),
        "graph_scaffold_removable": scaffold_loc(GRAPH_PY, root=root),
        "droppable_deps": droppable_deps(root=root),
    }


ERGONOMICS_NOTE = """\
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
"""


def render_markdown(data: dict, root: Path = REPO) -> str:
    deps = data["droppable_deps"]
    lines = [
        "# LangGraph-Removal Spike — Engineering Scorecard",
        "",
        "> Decider part (ii). Behavior ties (Task 8 parity gate), so this + the",
        "> Task 9 fault-injection resilience A/B are the decision inputs.",
        "",
        "## LOC delta",
        "",
        f"- Lite code added: **{data['lite_loc_added']} LOC** "
        f"({' + '.join(str(count_lines(m, root)) for m in LITE_MODULES)} across 4 new modules "
        f"+ ~{INFILE_LITE_LOC} in-file for the approval channel + `_run_engine`).",
        f"- `graph.py` total: {data['graph_py_total']} LOC.",
        f"- LangGraph scaffolding removable-if-adopted: **~{data['graph_scaffold_removable']} LOC** "
        "(the `StateGraph` wiring, `add_node`/`add_edge`/`add_conditional_edges`, `.compile`, "
        "`interrupt`, and the two checkpointer helpers).",
        "",
        "  **Honesty note:** graph.py's node *bodies* (assess-ambiguity, reflect, etc.) are LOGIC "
        "that lite REPRODUCES — they are not dead weight removed. The genuine removal is the "
        "graph *scaffolding* + the checkpointer SQLite layer, not all "
        f"{data['graph_py_total']} lines of graph.py.",
        "",
        "## Droppable dependencies",
        "",
        "Removable from `services/orchestrator/requirements.txt` once `graph.py` + its checkpointer go:",
        "",
    ]
    lines += [f"- `{d}`" for d in deps] or ["- (none found)"]
    lines += ["", ERGONOMICS_NOTE]
    return "\n".join(lines) + "\n"


def main() -> None:
    data = build_scorecard()
    out = REPO / "eval/orchestrator_ab/SCORECARD.md"
    out.write_text(render_markdown(data))
    print(f"wrote {out} — {data}")


if __name__ == "__main__":
    main()
