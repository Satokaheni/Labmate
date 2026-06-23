---
name: commit-pr
description: >-
  Authors commit messages and pull-request descriptions from a working-tree diff.
  summarize_diff groups changes by intent; write_commit emits a Conventional
  Commits message; write_pr produces a PR body with summary, rationale, test
  plan, and risk notes. Use after editing code, when preparing a commit, or when
  opening a pull request. Reads the diff only — it never stages, commits, or
  pushes. Distinct from the git_status/git_log/git_diff bridge tools, which only
  read repository state; this skill generates the prose that describes a change.
version: "0.1.0"
license: MIT
requires: []
---

# commit-pr Skill

Generates the prose describing a change — never mutates the repository.

## When to use

- After editing code, to prepare a commit message.
- When opening a pull request.

## Tools

- `summarize_diff(diff_text=None, repo_path=None)` — groups changes by intent;
  runs read-only `git diff HEAD` when no diff is passed.
- `write_commit(groups, scope=None)` — `{message}` in Conventional Commits format.
- `write_pr(groups, title=None)` — `{title, body}` with Summary, Rationale,
  Test Plan, and Risk Notes sections.

## Constraints

- NEVER runs `git add`, `git commit`, or `git push`. Reads the diff only.
