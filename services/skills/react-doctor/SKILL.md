---
name: react-doctor
description: >
  Deterministic static analysis of React codebases via react-doctor CLI. Checks
  State & Effects, Performance, Architecture, Security, and Accessibility rules.
  Use as a QA gate after any React code generation to catch common anti-patterns
  before returning code to the user. Returns issues with stable rule IDs.
trigger: "Use after generating or modifying React code to catch anti-patterns"
tools:
  - audit
  - list_rules
version: "0.1.0"
license: MIT
requires: []
---

# react-doctor

Wraps the [react-doctor](https://github.com/millionco/react-doctor) CLI as an MCP
server. Purely deterministic static analysis — no LLM inference.

## Tools

### `audit(project_path, rules?, ci_mode?)`

Run react-doctor on a React project root. Returns a summary line followed by JSONL,
one issue per line. Each issue has:

- `rule_id` — stable ID, e.g. `react-doctor/no-array-index-as-key`
- `category` — one of `state_effects`, `performance`, `architecture`, `security`, `accessibility`
- `severity` — `error`, `warning`, or `info`
- `file`, `line`, `column`
- `message`

Parameters:
- `project_path` (required) — absolute path to the project root
- `rules` (optional) — restrict the audit to specific rule IDs
- `ci_mode` (optional, default false) — report only newly introduced issues vs the baseline

On a missing CLI or unexpected failure, returns a structured error object
(`{ "error": true, ... }`) with `isError: true` — it never crashes.

### `list_rules()`

Returns all available rule IDs grouped by category.

## Configuration

- `REACT_DOCTOR_CMD` — override the CLI invocation. Defaults to `npx react-doctor@latest`.
  Example: `pnpm dlx react-doctor`.

## Usage notes

react-doctor exits non-zero when issues are found; this is treated as a successful
audit (issues are returned), not an error. Only an unspawnable CLI or unparseable
output yields an error result.
