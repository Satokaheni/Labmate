---
name: repo-fault-localize
description: >
  Hierarchical fault localization for bug fixing: narrows from repository → file →
  function → edit location using Agentless-style BM25+LLM ranking. Use as the first
  step in any bug-fix workflow to identify exactly where to make changes before editing.
  Three-stage pipeline: locate_files → locate_symbols → suggest_edit_sites.
trigger: "Use at the start of a bug-fix task to identify which files and functions to edit"
tools:
  - fault_localize.locate_files
  - fault_localize.locate_symbols
  - fault_localize.suggest_edit_sites
version: "0.1.0"
license: MIT
requires: [repo-graph]
---

# Repo Fault Localize Skill

You have access to the `fault_localize` MCP server which performs Agentless-style
hierarchical fault localization: given a bug description it narrows the search from
the whole repository down to specific edit sites in three staged passes.

## When to Use

- At the **start of any bug-fix task**, before opening or editing files.
- When a traceback or issue names a behavior but not a location.
- To produce a short, ranked list of edit candidates for a downstream patcher.

## Pipeline

1. `locate_files(issue, repo_path)` → top-k suspect files (BM25 + LLM rerank).
2. For the top file(s): `locate_symbols(issue, file, repo_path)` → suspect
   functions/classes with line ranges.
3. `suggest_edit_sites(issue, file, symbols)` → precise line-range edit hunks.

Stop at any stage to inspect, then drive the next stage with the chosen inputs.

## Available Tools

### `fault_localize.locate_files(issue, repo_path, top_k=5)`
Return the top-k files most likely to need edits. JSONL with `file`, `score`, `reason`.

### `fault_localize.locate_symbols(issue, file, repo_path)`
Within a located file, return the functions/classes most likely to contain the bug.
JSONL with `file`, `symbol`, `kind`, `start_line`, `end_line`, `reason`.

### `fault_localize.suggest_edit_sites(issue, file, symbols)`
Return specific line ranges (edit hunks) within the given symbols. JSONL with
`file`, `start_line`, `end_line`, `reason`.

## Workflow

Always run the stages in order. Feed the chosen file from stage 1 into stage 2,
and the chosen symbol names from stage 2 into stage 3.
