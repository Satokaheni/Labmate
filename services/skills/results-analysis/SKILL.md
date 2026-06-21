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
