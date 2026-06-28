# Contributing

## Branch protection (local-only)

The repo is private on the free plan, where GitHub server-side **rulesets are
unavailable**. Protection is therefore client-side + CI signal:

- **Never push directly to `main`** — open a PR from a feature branch. A
  `pre-push` hook blocks direct pushes to `main` (override only in emergencies
  with `git push --no-verify`).
- **CI** runs on every PR (`.github/workflows/ci.yml`): ruff on changed Python,
  frontend `tsc` type-check, and the orchestrator test suite. It's a signal, not
  a hard gate (a required-check gate needs GitHub Pro / a public repo).

> When the repo goes Pro or public, the prepared ruleset (no direct push to
> `main`, PR required, force-push/delete blocked, CI as a required check) can be
> applied — see the team for the `gh api` payload.

## One-time setup

```bash
pip install pre-commit
pre-commit install                       # lint/format on commit
pre-commit install --hook-type pre-push  # block direct push to main
```

Optionally lint the whole tree once: `pre-commit run --all-files`.

## What runs on commit

- **ruff** (lint + autofix) and **ruff format** on staged Python files
  (config: `ruff.toml` — `E,F,I,UP,B`, line length 100).
- **frontend `tsc -b --noEmit`** when `services/frontend/**.ts(x)` is staged.
- Generic hygiene: trailing whitespace, end-of-file, YAML/JSON checks,
  merge-conflict markers, large-file guard.

Hooks run on **staged files only**, so existing code isn't reformatted unless you
touch it. Bypass a hook in an emergency with `git commit --no-verify`.

## Tests

```bash
PYTHONPATH=. python -m pytest tests/ -q          # full mocked suite (no GPU/services)
LIVE_TESTS=1 PYTHONPATH=. python -m pytest tests/live -v   # live seams (needs services / model)
```
