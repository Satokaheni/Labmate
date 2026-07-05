# Local eval harness (Redis-free)

A local, in-process eval harness for the single-process runtime
(`services.orchestrator.main.OrchestratorProcess`). Mined from
`~/Work/ml-intern`'s telemetry approach (see
`.superpowers/sdd/ml-intern-mining.md` §2): a programmatic **trajectory
tagger + KPI reducer**, no judge model, no Redis. The old `eval/seq_ab/`
harness is kept as reference only — it drives goals through a standalone
Redis stream to simulate a detached client (see
`eval/seq_ab/local_tool_responder.py`'s module docstring) and is stale
against the current single-process/SQLite architecture. Do not extend it;
extend this harness instead.

## Components

- **`score_trajectory.py`** — PURE, CI-tested core. `tag_run(traj) -> list[str]`
  (outcome/edited+verified/fabricated/doom_looped/tool:\<name\> tags) +
  `kpi_reduce(trajs) -> dict` (ok/edited-verified/fabricated/doom-loop/
  verify-nudge/tool-success rates, avg llm calls, ttfa/wall p50/p95, tag
  counts). No I/O — testable offline on fixture dicts
  (`tests/eval/test_score_trajectory.py`).
- **`cases.py`** — the case set + fixture file bodies, COPIED from
  `eval/seq_ab/run_seq_ab.py` (that module imports `redis` at the top, which
  would defeat this package's "no redis anywhere" requirement). Keep in sync
  by eye if the seq_ab case set changes.
- **`run_local_eval.py`** — the live runner (NOT run in CI; needs `GEMMA_BASE`
  up). Boots an `OrchestratorProcess`, pre-subscribes to each task's event-bus
  topic before `submit_goal` (same ordering as
  `services/ws_gateway/server.py::_handle_send` + `_relay_task`), drains the
  event stream into a trajectory dict, and scores the batch.
- **`compare_local.py`** — A/B compare two `report-<mode>.json` files using the
  EXISTING Wilson-CI tooling (`eval/seq_ab/compare.py` + `variance.py`, which
  do not import redis — verified, so no wrapper duplication of the CI math was
  needed beyond reshaping the per-case records).

## Running it

Model server must be up first (`infrastructure/local/serve-model.sh`).

```bash
# One run, default mode label (skill_first); writes eval/local/report-skill_first.json
PYTHONPATH=. python -m eval.local.run_local_eval

# Multiple trials per case (recommended — c1/c3-style compound cases are known
# to flake on the Q4 model; see CLAUDE.md's Agentic Fix Loop section)
PYTHONPATH=. python -m eval.local.run_local_eval --trials 3

# Label a react-mode run's output files (SEQUENCING_MODE is read by the
# orchestrator at IMPORT time, process-wide — set the env var before invoking,
# --mode only controls the output filename label)
SEQUENCING_MODE=react PYTHONPATH=. python -m eval.local.run_local_eval --mode react --trials 3
```

Output per run:
- `eval/local/trajectories-<mode>.jsonl` — one raw trajectory dict per line.
- `eval/local/report-<mode>.json` — `{"mode", "trajectories": [tagged], "kpis": {...}}`.

## A/B protocol

1. Run mode/change A → `report-A.json` (rename or move the default output so a
   second run doesn't overwrite it, e.g. `cp eval/local/report-skill_first.json
   eval/local/report-before.json`).
2. Run mode/change B → `report-B.json`.
3. Compare:
   ```bash
   PYTHONPATH=. python -m eval.local.compare_local eval/local/report-before.json eval/local/report-after.json
   ```
4. Read the verdict: a case is a `WIN` only when the variant's
   `outcome:completed` pass-rate is higher AND both arms have `>= min_trials`
   (default 5) AND their Wilson CIs are disjoint — a single green run or an
   overlapping-CI bump is NOT a win (same bar as
   `docs/superpowers/eval-variance-policy.md`).
5. Eyeball the KPI deltas the compare tool prints alongside the verdict:
   `fabricated_rate` must stay `0.0` in both arms, and `doom_loop_rate` /
   `tool_success_rate` / `avg_llm_calls` must not regress even when the
   pass-rate win bar isn't met.

## Why pass-rate is scored on `outcome:completed`

`tag_run`'s outcome tag is mutually exclusive
(`completed`/`doom_loop`/`cancelled`/`errored`), so scoring on
`outcome:completed` already folds in "didn't loop, wasn't cancelled, didn't
error" — it is the harness's honest single completion signal. `fabricated`
and `edited+verified` are reported as separate KPI rows precisely so a
regression in either shows up even when the raw pass-rate doesn't move (e.g.
an "ok=True" answer that is actually a fabricated claim is excluded from
`edited+verified` and flagged by `fabricated_rate`, not silently counted as a
clean win).
