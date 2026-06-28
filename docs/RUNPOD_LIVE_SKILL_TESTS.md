# RunPod handoff — live skill tests (continuing with the LLM)

Branch: **`feat/live-skill-tests`** (isolated from `feat/agentic-fix-loop`, which stays the A/B branch).

```bash
git fetch origin && git checkout feat/live-skill-tests && git pull
```

The **model-free** live suite is already green on a no-GPU host (58 passed, 2 skipped).
What remains is the part that needs the LLM: the **inference-guarded** execution tests
(`repo-fault-localize`, `code-review`, `critique`, `test-gen`). They `require_service`-skip
when `GEMMA_BASE` is unreachable — on RunPod, with the model up, they actually run.

---

## 1. Prerequisites on the RunPod host

```bash
# a) model serving (so GEMMA_BASE/health is 2xx)
infrastructure/local/serve-model.sh        # wait until healthy
curl -s http://localhost:8000/health | grep '"status"'   # -> "ok"
export GEMMA_BASE="http://localhost:8000/v1"   # the suite strips /v1 to hit /health

# b) build node skills (stale dist -> the suite SKIPS them with "run npm run build")
( cd services/skills/component-doc-gen && npm run build )
( cd services/skills/ast-ts-refactor   && npm run build )

# c) install per-skill Python deps so inference skills can register
#    (code-review etc. import litellm + instructor; if a skill SKIPS with
#     "registration failed", install its requirements and retry)
pip install -r services/skills/code-review/requirements.txt
# ...repeat for any skill that skips with a register/import error
```

## 2. Run the suites

```bash
# Model-free (no GPU) — should stay all green; re-run as a sanity check
LIVE_TESTS=1 PYTHONPATH=. python -m pytest \
  tests/live/test_skill_harness.py \
  tests/live/test_skill_contract_live.py \
  tests/live/test_skill_unknown_tool_live.py \
  tests/live/test_skill_exec_modelfree_live.py -rs -q

# Inference-guarded (NEEDS GEMMA_BASE up) — this is the new coverage
LIVE_TESTS=1 PYTHONPATH=. python -m pytest \
  tests/live/test_skill_exec_inference_live.py -rs -v
```

## 3. How to read the results

- **PASS** — the skill's tool ran and returned a usable result.
- **SKIPPED** — a prerequisite is missing (model down, skill deps absent, skill not runnable).
  The skip reason says which; fix the prereq (§1) and re-run. A skip is NOT a green tool.
- **FAIL** — distinguish two cases from the assertion message:
  - **Discovered skill bug** (the valuable kind): the tool ran but returned a wrong/empty/punt
    result. The flagship guard is `test_repo_fault_localize_does_not_punt_on_tiny_file` — it
    fails if `repo-fault-localize` returns the **"file too large"** punt on a 7-line fixture
    (the bug seen in the A/B). Fix the skill in `services/skills/<name>/`.
  - **Test bug**: a wrong arg key vs the skill's real `server.py` schema, or a too-strict
    assertion. Fix the test, not the skill. (Each inference test has an implementer note about
    verifying arg names against `services/skills/<name>/server.py`.)

## 4. The fix loop (same as we used for the model-free side)

For each FAIL: decide skill-bug vs test-bug, make the minimal fix in the one file, re-run
that test until green, commit (one fix per commit, explicit `git add <path>` — never `-A`).
Keep going until `test_skill_exec_inference_live.py` is all PASS/SKIP with no FAILs.

```bash
git add <the one file you fixed>
git commit -m "fix(<skill>): <what> (found by live skill suite)"
git push origin feat/live-skill-tests
```

## 5. What to send back

Paste the `-rs` summary line + any FAIL's assertion message (and which case). I'll triage
skill-bug vs test-bug and fold the fixes in. Top thing I'm watching for: whether
`repo-fault-localize` punts "file too large" with the model actually wired up.

---

### Notes
- `dist/` is git-ignored (built per-deploy), so a node-skill fix is in `src/` — rebuild before testing.
- The suite spawns each skill's MCP subprocess in-process (no Redis/worker needed); only the
  model itself needs `GEMMA_BASE`.
- Reference: `docs/superpowers/plans/2026-06-27-live-skill-contract-suite.md` and
  `...-execution-smoke.md`; full runner usage is in `CLAUDE.md` §12.
