# REVIEW.md — Judge Spec for the Labmate Review Step

This file is the rubric for the **review/judge step** of the Implementation
Workflow (CLAUDE.md → "Implementation Workflow": Haiku/Sonnet implements → Opus
judges). A reviewing agent — human or model — grades a diff against these tiers
and reports findings by severity. Ported from the ml-intern `REVIEW.md` pattern
(mining G9) and specialized to Labmate's harness concerns.

The reviewer's job is to **find the smallest set of real defects that must change
before merge**, ranked by severity, each with a concrete failure scenario. Not a
style pass; not a rewrite. If nothing survives verification, say so plainly.

---

## Severity tiers

### P0 — Blocker (must fix before merge)
A defect that is wrong, unsafe, or breaks the build/contract. Merging it causes
incorrect behavior, data loss, a crash, a security hole, or a red main.

- Logic that produces a wrong result for a realistic input.
- A crash / unhandled exception on a reachable path.
- A test that asserts nothing, is tautological, or is hard-coded to pass.
- A regression: an existing behavior or test broken by the change.
- **Honesty regression (Labmate-specific):** any path that lets the harness
  report `ok=True` / "I fixed it, tests pass" without a verified passing run
  this turn — the `completion_guard.reconcile_ok` / `verification_stop` /
  continuation-guard invariants must hold. Fabricated completion is a P0.
- **stdout in an MCP server** (`print`/`console.log`) — corrupts JSON-RPC.
- **Prompt-cache regression (Labmate-specific):** volatile content (date,
  random ids, per-turn text) injected into the byte-stable system+tools prefix,
  busting the llama.cpp SWA prefix cache. Volatile data belongs in the goal/user
  message, never the cached prefix.
- Violates a CLAUDE.md hard rule (tiktoken, `chromadb.PersistentClient`,
  `asyncio.run()` in async context, Discord wired into an active path,
  `thinking_budget_tokens` unset on a model call, Mongo/Redis reintroduced into
  the local single-process harness).
- A concurrency bug: shared mutable state mutated without coordination on a
  reachable interleaving (e.g. the two-writer LocalStore case).

### P1 — Important (fix before merge unless explicitly deferred)
A defect that is real but narrower in blast radius, or a correctness risk that
depends on an input the change does not yet handle.

- An unhandled edge case that is plausible but not the common path.
- A missing test for a branch the change introduces (esp. an error/guard path).
- An off-by-one, a boundary, or a resource that is not bounded (an unbounded
  loop/retry with no iteration/wall-clock/no-progress ceiling — Labmate bounds
  these deliberately; a new loop must too).
- A magic number or duplicated logic block that will drift.
- An env knob added without a default, or whose default changes behavior
  silently (document default-behavior changes, like ROUTE_EDIT_TO_REACT).
- A repair/guard that is not idempotent or not a safe no-op when its trigger is
  absent (message-repair, self-heal, verification-stop must all no-op cleanly).

### P2 — Minor (note; fix opportunistically)
Real but low-impact: naming, a comment that is stale or wrong, a docstring that
overstates scope, a slightly weak (but not tautological) assertion, dead code in
a touched file. Record these; do not block merge on them alone.

---

## What the reviewer checks (Labmate lens)

Copy the change's **Global Constraints** (from its task brief / plan) into the
review as the attention lens, then work through:

1. **Spec compliance** — does it do exactly what the task said, no more (no
   unrequested feature), no less (no missing requirement)?
2. **Correctness** — trace one realistic input end-to-end. Where does it break?
3. **Honesty invariants** — for any change in `coding_orchestrator.py`,
   `completion_guard.py`, `verification_stop.py`, `error_classifier.py`: can the
   harness now claim success it didn't achieve, or retry a terminal failure to
   exhaustion, or bypass a verification gate?
4. **Boundedness** — every loop/retry has an iteration, wall-clock, or
   no-progress ceiling; refunds/grace are accounted; nothing can spin forever.
5. **Purity & idempotence** — pure helpers don't do I/O or mutate inputs;
   guards/repairs are safe no-ops when not triggered and idempotent under repeat.
6. **Prefix-cache stability** — the system+tools prefix stays byte-stable per
   goal; no volatile content leaks into it.
7. **Test hygiene** — tests assert structure/behavior (not literal LLM text),
   actually exercise the new branch, and fail before the fix. A test that
   passes against the pre-change code is not a test of the change.
8. **Blast radius** — does the change touch a load-bearing shared path
   (`_run_react_loop`, LocalStore, `_maybe_repair`, model_client) in a way that
   affects callers the diff doesn't show? Flag "⚠️ cannot verify from diff" for
   the controller to resolve with cross-task context.

---

## Reporting format

Report findings most-severe first. For each:

```
[P0|P1|P2] <one-sentence defect> — <file>:<line>
Failure scenario: <concrete input/state → wrong output/crash>
Fix: <one line>
```

Then a verdict:

- **PASS** — no P0, no unaddressed P1. Safe to merge.
- **CHANGES NEEDED** — one or more P0, or a P1 the author hasn't chosen to
  defer. List them; the author fixes and re-review.

If a finding conflicts with what the plan explicitly mandated, do **not**
silently override it — surface the finding beside the plan text and let the
human decide which governs.

Verify before reporting: a finding you can't tie to a concrete failing input is
a hypothesis, not a defect. Label unverified hypotheses as such or drop them.
