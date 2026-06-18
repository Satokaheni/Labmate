---
name: repo-graph
description: >
  Line-level repository code graph providing cross-file reference and call edges.
  Use when you need to find all callers of a function, all references to a symbol,
  or understand the dependency structure of a codebase at function granularity.
  Complements ast-repo-map (file-level ranking) with precise cross-file edges.
trigger: "Use when tracing function calls, finding all usages, or understanding cross-file dependencies"
tools:
  - repo_graph.build
  - repo_graph.search
  - repo_graph.get_references
  - repo_graph.get_callers
  - repo_graph.get_callees
version: "0.1.0"
license: MIT
requires: []
---

# Repo Graph Skill

You have access to the `repo_graph` MCP server which builds a line-level code
graph of a repository: cross-file edges connecting where each symbol is defined
to every place it is referenced or called.

## When to Use

- Find every caller of a function before changing its signature
- Find all references to a symbol across files (impact analysis)
- Trace what a function calls (its dependencies)
- Search for symbols by name when you do not know the file

This complements `ast-repo-map`: that skill ranks *which files* matter; this
skill answers *how symbols connect* at function granularity.

## Available Tools

### `repo_graph.build(repo_path)`
Build or update the graph for a repo. Run this once before querying. Returns
summary stats (files parsed, definitions, edges by kind).

### `repo_graph.search(query, top_k=10)`
Find symbols matching `query` by name. Returns JSONL of matches with
`file:line` locations.

### `repo_graph.get_references(file, symbol)`
Every site that references `symbol` defined in `file`. Returns JSONL.

### `repo_graph.get_callers(file, symbol)`
Functions/methods that call `symbol`. Returns JSONL.

### `repo_graph.get_callees(file, symbol)`
Functions/methods that `symbol` calls. Returns JSONL.

## Workflow

1. Call `repo_graph.build` once at the start.
2. Use `search` to locate a symbol, then `get_callers` / `get_callees` /
   `get_references` to traverse the graph.
