# B4 results-analysis Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan.

**Goal:** Add a `results-analysis` skill that profiles local experiment result files, runs significance tests across configs, and emits publication-ready figures and tables.

**Architecture:** A standard Labmate skill under `services/skills/results-analysis/`: `SKILL.md`, a logic module `results_analysis.py` with three functions, and an MCP `server.py`. The analysis runs directly with pandas/scipy/matplotlib (trusted skill code, not agent-generated, so no sandbox round-trip is needed for now). `SkillRunner.discover()` catalogs it from frontmatter.

**Tech Stack:** Python, `mcp` SDK, pandas, scipy, matplotlib (Agg backend), pytest.

> **Skill rules:** Never `print()` — log to `sys.stderr`. `server.py` uses `mcp.server.Server` + `stdio_server`. Use matplotlib's non-interactive Agg backend (`matplotlib.use("Agg")` before importing pyplot) so figures render headless. No litellm calls in this skill — it is deterministic data analysis.

---

### Task 1: Create the skill logic module

**Files:**
- Create: `services/skills/results-analysis/results_analysis.py`
- Create: `services/skills/results-analysis/__init__.py`
- Create: `tests/services/skills/results-analysis/__init__.py`
- Create: `tests/services/skills/results-analysis/test_results_analysis.py`

- [ ] **Step 1: Write the failing test**

Create `tests/services/skills/results-analysis/__init__.py` (empty), then `tests/services/skills/results-analysis/test_results_analysis.py`:

```python
from __future__ import annotations
import csv
import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "services" / "skills" / "results-analysis" / "results_analysis.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("results_analysis", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def test_profile_results_detects_metric_columns(tmp_path):
    ra = _load()
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"id": "1", "config": "a", "accuracy": "0.80", "f1": "0.75"},
        {"id": "2", "config": "b", "accuracy": "0.90", "f1": "0.85"},
    ])
    out = ra.profile_results(str(csv_path))
    assert out["row_count"] == 2
    assert "accuracy" in out["columns"]
    # Numeric non-id columns are detected as metrics with stats.
    assert "accuracy" in out["stats"]
    assert "mean" in out["stats"]["accuracy"]


def test_compare_runs_returns_pvalue_and_ci(tmp_path):
    ra = _load()
    csv_path = tmp_path / "runs.csv"
    rows = []
    for i in range(8):
        rows.append({"config": "baseline", "score": str(0.50 + i * 0.001)})
    for i in range(8):
        rows.append({"config": "treatment", "score": str(0.70 + i * 0.001)})
    _write_csv(csv_path, rows)
    out = ra.compare_runs(str(csv_path), group_col="config", metric_col="score")
    assert "p_value" in out
    assert isinstance(out["confidence_interval"], list)
    assert len(out["confidence_interval"]) == 2
    assert out["significant"] is True  # clearly separated groups


def test_make_figures_writes_files_and_tables(tmp_path):
    ra = _load()
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"config": "a", "accuracy": "0.80"},
        {"config": "b", "accuracy": "0.90"},
    ])
    out_dir = tmp_path / "figs"
    out = ra.make_figures(str(csv_path), metric_cols=["accuracy"], output_dir=str(out_dir))
    assert out["figure_paths"]
    assert all(Path(p).exists() for p in out["figure_paths"])
    assert "accuracy" in out["markdown_table"]
    assert "accuracy" in out["latex_table"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/skills/results-analysis/test_results_analysis.py -v`
Expected: FAIL (module does not exist)

- [ ] **Step 3: Write minimal implementation**

Create `services/skills/results-analysis/__init__.py` (empty).

Create `services/skills/results-analysis/results_analysis.py`:

```python
"""results-analysis skill logic: profile, compare, and visualize result files.

CRITICAL: never write to stdout. All logging goes to stderr.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # headless backend — must precede pyplot import
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("results-analysis")

_ID_HINTS = ("id", "index", "seed", "run", "step", "epoch")


def _read(file_path: str) -> pd.DataFrame:
    suffix = Path(file_path).suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(file_path)
    if suffix in (".jsonl", ".ndjson"):
        return pd.read_json(file_path, lines=True)
    if suffix == ".json":
        return pd.read_json(file_path)
    if suffix in (".parquet", ".pq"):
        return pd.read_parquet(file_path)
    raise ValueError(f"unsupported file type: {suffix}")


def _metric_columns(df: pd.DataFrame) -> list[str]:
    """Numeric columns whose name is not an obvious id/index column."""
    metrics = []
    for col in df.columns:
        if any(h in str(col).lower() for h in _ID_HINTS):
            continue
        if pd.api.types.is_numeric_dtype(df[col]):
            metrics.append(col)
    return metrics


def profile_results(file_path: str) -> dict:
    """Descriptive stats + auto-detected metric columns."""
    df = _read(file_path)
    metrics = _metric_columns(df)
    stats_out: dict = {}
    for col in metrics:
        s = df[col].dropna()
        stats_out[col] = {
            "mean": float(s.mean()) if len(s) else None,
            "std": float(s.std()) if len(s) else None,
            "min": float(s.min()) if len(s) else None,
            "max": float(s.max()) if len(s) else None,
            "count": int(s.count()),
        }
    return {
        "columns": [str(c) for c in df.columns],
        "stats": stats_out,
        "row_count": int(len(df)),
    }


def compare_runs(file_path: str, group_col: str, metric_col: str) -> dict:
    """Paired-ish significance test + bootstrap CI of the difference of means."""
    df = _read(file_path)
    groups = {str(k): g[metric_col].dropna().to_numpy()
              for k, g in df.groupby(group_col)}
    keys = list(groups)
    if len(keys) < 2:
        return {"groups": {k: v.tolist() for k, v in groups.items()},
                "p_value": 1.0, "confidence_interval": [0.0, 0.0],
                "significant": False}
    a, b = groups[keys[0]], groups[keys[1]]
    t_stat, p_value = stats.ttest_ind(a, b, equal_var=False)

    # Bootstrap CI of the difference in means (b - a).
    rng = np.random.default_rng(0)
    diffs = []
    for _ in range(2000):
        ra = rng.choice(a, size=len(a), replace=True)
        rb = rng.choice(b, size=len(b), replace=True)
        diffs.append(rb.mean() - ra.mean())
    lo, hi = np.percentile(diffs, [2.5, 97.5])
    return {
        "groups": {k: v.tolist() for k, v in groups.items()},
        "p_value": float(p_value),
        "confidence_interval": [float(lo), float(hi)],
        "significant": bool(p_value < 0.05),
    }


def make_figures(file_path: str, metric_cols: list[str], output_dir: str) -> dict:
    """Bar plots per metric + markdown/LaTeX summary tables."""
    df = _read(file_path)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    figure_paths: list[str] = []
    for col in metric_cols:
        if col not in df.columns:
            continue
        fig, ax = plt.subplots()
        df[col].plot(kind="bar", ax=ax)
        ax.set_title(col)
        ax.set_ylabel(col)
        path = out / f"{col}.png"
        fig.tight_layout()
        fig.savefig(path, dpi=150)
        plt.close(fig)
        figure_paths.append(str(path))

    summary = df[metric_cols].describe().transpose()
    summary.index.name = "metric"
    markdown_table = summary.reset_index().to_markdown(index=False)
    latex_table = summary.reset_index().to_latex(index=False)
    return {
        "figure_paths": figure_paths,
        "markdown_table": markdown_table,
        "latex_table": latex_table,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/skills/results-analysis/test_results_analysis.py -v`
Expected: PASS

(Note: `to_markdown` requires the `tabulate` package, which pandas pulls in as an optional dep. If the test errors on `tabulate`, add `tabulate>=0.9.0` to requirements and install it.)

- [ ] **Step 5: Commit**

```bash
git add services/skills/results-analysis/__init__.py services/skills/results-analysis/results_analysis.py tests/services/skills/results-analysis/
git commit -m "feat(skills): add results-analysis logic module (B4)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Add SKILL.md, server.py, and requirements.txt

**Files:**
- Create: `services/skills/results-analysis/SKILL.md`
- Create: `services/skills/results-analysis/server.py`
- Create: `services/skills/results-analysis/requirements.txt`

- [ ] **Step 1: Write the failing test**

Append to `tests/services/skills/results-analysis/test_results_analysis.py`:

```python
def test_skill_md_frontmatter_parses():
    import frontmatter
    skill_md = _MODULE_PATH.parent / "SKILL.md"
    assert skill_md.exists()
    meta, _ = frontmatter.parse(skill_md.read_text(encoding="utf-8"))
    assert meta["name"] == "results-analysis"
    assert meta["requires"] == ["code-sandbox"]


def test_server_lists_three_tools():
    import importlib.util, asyncio
    server_path = _MODULE_PATH.parent / "server.py"
    spec = importlib.util.spec_from_file_location("results_analysis_server", server_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tools = asyncio.get_event_loop().run_until_complete(mod.list_tools())
    assert {t.name for t in tools} == {"profile_results", "compare_runs", "make_figures"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/services/skills/results-analysis/test_results_analysis.py::test_skill_md_frontmatter_parses tests/services/skills/results-analysis/test_results_analysis.py::test_server_lists_three_tools -v`
Expected: FAIL

- [ ] **Step 3: Write minimal implementation**

Create `services/skills/results-analysis/SKILL.md` (frontmatter pasted EXACTLY):

```markdown
---
name: results-analysis
description: >-
  Analyzes local experiment result files (CSV/JSONL/Parquet of metrics, eval
  scores, ablations) and produces summary tables, significance tests, and
  figures. profile_results computes descriptive stats and auto-detects metric
  columns; compare_runs runs paired significance tests across configurations;
  make_figures emits publication-ready matplotlib/plotly plots and a
  markdown/LaTeX results table. Use when summarizing eval outputs, comparing
  experimental configurations, or building a results table or figure for a paper.
  Executes inside code-sandbox for isolation. Distinct from paper-rag (literature
  evidence) and web-search (external info) — this operates on local result files.
version: "0.1.0"
license: MIT
requires: ["code-sandbox"]
---

# results-analysis Skill

Turns local experiment result files into tables, significance tests, and figures.

## When to use

- Summarizing eval outputs or ablation result files.
- Comparing experimental configurations with a significance test.
- Building a results table or figure for a paper.

## Tools

- `profile_results(file_path)` — `{columns, stats, row_count}`; auto-detects
  numeric metric columns.
- `compare_runs(file_path, group_col, metric_col)` — `{groups, p_value,
  confidence_interval, significant}` (Welch t-test + bootstrap CI).
- `make_figures(file_path, metric_cols, output_dir)` — `{figure_paths,
  markdown_table, latex_table}`.

## Constraints

- Reads CSV / JSONL / Parquet local files only.
- Uses the matplotlib Agg backend (headless). Deterministic data analysis.
```

Create `services/skills/results-analysis/server.py`:

```python
"""MCP server for the results-analysis skill (stdio JSON-RPC).

CRITICAL: stdout carries JSON-RPC. All logging goes to stderr.
"""
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool
import asyncio
import json
import logging
import sys

import results_analysis

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("results-analysis.server")
app: Server = Server("results-analysis")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="profile_results",
             description="Descriptive stats + auto-detected metric columns.",
             inputSchema={"type": "object",
                          "properties": {"file_path": {"type": "string"}},
                          "required": ["file_path"]}),
        Tool(name="compare_runs",
             description="Significance test + bootstrap CI across configurations.",
             inputSchema={"type": "object", "properties": {
                 "file_path": {"type": "string"},
                 "group_col": {"type": "string"},
                 "metric_col": {"type": "string"}},
                 "required": ["file_path", "group_col", "metric_col"]}),
        Tool(name="make_figures",
             description="Publication-ready figures + markdown/LaTeX tables.",
             inputSchema={"type": "object", "properties": {
                 "file_path": {"type": "string"},
                 "metric_cols": {"type": "array", "items": {"type": "string"}},
                 "output_dir": {"type": "string"}},
                 "required": ["file_path", "metric_cols", "output_dir"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "profile_results":
            result = results_analysis.profile_results(arguments["file_path"])
        elif name == "compare_runs":
            result = results_analysis.compare_runs(
                arguments["file_path"], arguments["group_col"], arguments["metric_col"])
        elif name == "make_figures":
            result = results_analysis.make_figures(
                arguments["file_path"], arguments["metric_cols"], arguments["output_dir"])
        else:
            raise ValueError(f"unknown tool: {name}")
        return [TextContent(type="text", text=json.dumps(result, default=str))]
    except Exception as exc:
        log.exception("tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]


async def main() -> None:
    async with stdio_server() as (r, w):
        await app.run(r, w, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
```

Create `services/skills/results-analysis/requirements.txt`:

```
mcp>=1.0.0
pandas>=2.0.0
scipy>=1.10.0
matplotlib>=3.7.0
tabulate>=0.9.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/services/skills/results-analysis/test_results_analysis.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/skills/results-analysis/SKILL.md services/skills/results-analysis/server.py services/skills/results-analysis/requirements.txt
git commit -m "feat(skills): add results-analysis SKILL.md + MCP server (B4)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

### Task 3: Verify catalog registration

**Files:**
- Modify: `tests/services/skills/results-analysis/test_results_analysis.py`

Discovery is automatic from SKILL.md frontmatter. This guard test proves `SkillRunner` catalogs the skill and `catalog_prompt()` advertises it.

- [ ] **Step 1: Write the failing test**

Append to `tests/services/skills/results-analysis/test_results_analysis.py`:

```python
def test_skill_runner_catalogs_results_analysis():
    from services.skill_runner.skill_runner import SkillRunner
    skills_root = _MODULE_PATH.resolve().parent.parent  # .../services/skills
    runner = SkillRunner(roots=[skills_root])
    runner.discover()
    assert "results-analysis" in runner.catalog
    prompt = runner.catalog_prompt()
    assert "results-analysis" in prompt
    assert "significance" in prompt.lower()
```

- [ ] **Step 2: Run test to verify it passes**

Run: `pytest tests/services/skills/results-analysis/test_results_analysis.py::test_skill_runner_catalogs_results_analysis -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/services/skills/results-analysis/test_results_analysis.py
git commit -m "test(skills): assert results-analysis is cataloged (B4)

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```
