"""Fault-injection resilience A/B: graph engine vs lite engine.

Part of the LangGraph-removal spike (decider part i). The `lite` engine and
the `graph` engine tie behaviorally (both call the same `react_execute`), so
the deciding question is RESILIENCE: when the host process is killed
mid-goal and restarted, does each engine recover correctly?

  - `graph` resumes from its AsyncSqliteSaver checkpoint, keyed by thread_id
    (see services/orchestrator/graph.py::_make_async_sqlite_checkpointer).
  - `lite` resumes from services/orchestrator/lite_persistence.py::load_resume
    (a LocalStore-backed snapshot taken at each suspendable boundary) and, if
    the goal was suspended awaiting human approval, re-awaits that approval
    via services/orchestrator/inproc_bus.py::SignalRegistry.await_approval.

This module has two parts:

  Part A — a PURE scorer (``score_recovery`` / ``summarize``) over
  ``FaultTrajectory`` records. This is the CI-tested substance of the task:
  no I/O, no orchestrator imports, no GPU. It answers "given a recorded
  kill+restart trajectory, did the engine recover?"

  Part B — a RunPod-only live runner (``run_live`` / ``main``) that actually
  submits a goal, kills the process at a chosen point, restarts it, and
  observes whether/how it resumed. This half needs a real orchestrator + a
  model box and is NOT exercised in CI — it is gated behind LIVE_TESTS=1 (or
  --live) and lazily imports orchestrator internals only inside the gated
  path, so importing this module (or running the pure-scorer tests) never
  touches services.orchestrator.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

# Kill points: where the process was killed during the goal.
KILL_POINTS = ("before_execute", "mid_execute", "at_approval")


@dataclass(frozen=True)
class FaultTrajectory:
    """Record of one kill+restart run of one engine on one goal."""

    engine: str  # "graph" | "lite"
    kill_point: str  # one of KILL_POINTS
    turns_before_kill: int  # react-loop turns completed before the kill
    resumed: bool  # did the process come back up and pick the goal up at all?
    resumed_from_turn: (
        int  # the turn index execution continued from after restart (0 = restarted from scratch)
    )
    final_ok: bool  # did the goal finalize ok after restart?
    expected_ok: bool  # the ground-truth expected outcome for this goal


@dataclass(frozen=True)
class RecoveryScore:
    recovered: bool  # resumed AND finalized (final_ok matches expected)
    work_redone: bool  # lost progress: resumed_from_turn < turns_before_kill
    final_correct: bool  # final_ok == expected_ok
    resume_turn: int  # where it continued from (for reporting)


def score_recovery(t: FaultTrajectory) -> RecoveryScore:
    """Pure scorer: no I/O. Turns a kill+restart trajectory into a recovery verdict.

    - recovered: the process resumed the goal AND produced the correct terminal outcome.
    - work_redone: it resumed but from an earlier turn than it had reached (progress lost) —
      the key checkpoint-quality signal separating the two engines.
    - final_correct: the post-restart terminal ok matches the expected ok.
    """
    if t.kill_point not in KILL_POINTS:
        raise ValueError(f"unknown kill_point: {t.kill_point!r} (expected one of {KILL_POINTS})")
    final_correct = t.final_ok == t.expected_ok
    recovered = t.resumed and final_correct
    work_redone = t.resumed and t.resumed_from_turn < t.turns_before_kill
    return RecoveryScore(
        recovered=recovered,
        work_redone=work_redone,
        final_correct=final_correct,
        resume_turn=t.resumed_from_turn,
    )


def summarize(trajectories: list[FaultTrajectory]) -> dict:
    """Aggregate per-engine recovery stats for the A/B report."""
    out: dict[str, dict] = {}
    for t in trajectories:
        s = score_recovery(t)
        agg = out.setdefault(
            t.engine, {"runs": 0, "recovered": 0, "work_redone": 0, "final_correct": 0}
        )
        agg["runs"] += 1
        agg["recovered"] += int(s.recovered)
        agg["work_redone"] += int(s.work_redone)
        agg["final_correct"] += int(s.final_correct)
    return out


# ---------------------------------------------------------------------------
# Part B — RunPod-only live kill+restart runner (scaffold, NOT CI-run).
#
# Nothing below this line is exercised by CI. It is gated behind LIVE_TESTS=1
# (or --live) and every orchestrator import is lazy (inside run_live), so a
# plain `import eval.orchestrator_ab.run_fault_ab` never touches
# services.orchestrator / the GPU / a model box.
# ---------------------------------------------------------------------------


def _live_gate_message() -> str:
    return (
        "fault-injection live run is RunPod-only; set LIVE_TESTS=1 and run on "
        "a host with a model box (see run_fault_ab.py docstring for the manual "
        "kill+restart procedure)."
    )


async def run_live(engine: str, kill_point: str, n: int = 1) -> FaultTrajectory:
    """Live kill+restart procedure for one (engine, kill_point) pair.

    RunPod-only. Requires LIVE_TESTS=1. Intended procedure (documented here
    since a faithful in-process kill of the *current* process is impractical
    to script — a real run needs a subprocess/host-level kill signal):

      1. Start an OrchestratorProcess (services/orchestrator/main.py) for
         `engine` ("graph" routes through graph.py's LangGraph build with
         _make_async_sqlite_checkpointer; "lite" routes through
         lite_orchestrator.py + lite_persistence.py).
      2. Submit a goal via OrchestratorProcess.submit_goal(...) whose
         expected terminal `ok` is known ahead of time (ground truth).
      3. Let the ReAct loop run until it reaches the requested `kill_point`:
           - "before_execute": kill before the first tool-executing turn.
           - "mid_execute":    kill partway through the tool-calling loop
                                (after >=1 turn, before finish).
           - "at_approval":    kill while suspended awaiting human approval
                                (SignalRegistry.await_approval pending).
         Record `turns_before_kill` from the loop's own turn counter.
      4. Kill the process (SIGKILL the host process, not a graceful
         shutdown — the point is to test crash recovery, not clean exit).
      5. Restart a fresh OrchestratorProcess for the same `engine`.
      6. Observe whether the goal resumes:
           - graph: does the AsyncSqliteSaver checkpoint (keyed by
             thread_id == task_id) let the graph continue from a later
             node than START?
           - lite: does lite_persistence.load_resume(store, task_id) return
             a non-None (state, phase) pair, and if phase == "await_approval",
             does re-invoking SignalRegistry.await_approval(task_id) pick the
             suspended goal back up?
      7. Record `resumed`, `resumed_from_turn`, and `final_ok` (the terminal
         outcome after the restarted process finishes the goal, if it does),
         then return the FaultTrajectory.

    This body is intentionally a documented stub: a faithful kill+restart
    needs real process-level signals and a model box, which is exactly what
    "RunPod-only" means here. Prefer this explicit stub over a fake that
    pretends to kill and reports fabricated numbers.
    """
    raise NotImplementedError(
        "live kill+restart is run manually on RunPod — see run_live's docstring "
        "for the procedure (submit_goal -> kill at kill_point -> restart -> "
        "observe resume via the engine's checkpoint/resume path)."
    )


def main(argv: list[str] | None = None) -> int | None:
    """CLI entry point. Gated: does nothing (and imports nothing heavy) unless
    LIVE_TESTS=1 is set or --live is passed.
    """
    parser = argparse.ArgumentParser(
        description="Fault-injection resilience A/B: graph engine vs lite engine (RunPod-only)."
    )
    parser.add_argument("--engine", choices=("graph", "lite"), required=True)
    parser.add_argument("--kill-point", choices=KILL_POINTS, required=True)
    parser.add_argument("--n", type=int, default=1, help="number of trials to run")
    parser.add_argument(
        "--live",
        action="store_true",
        help="explicitly opt in to a live run (in addition to/instead of LIVE_TESTS=1)",
    )
    args = parser.parse_args(argv)

    if os.getenv("LIVE_TESTS") != "1" and not args.live:
        print(_live_gate_message())
        return 0

    # Only reached with LIVE_TESTS=1 or --live: safe to import orchestrator
    # internals and asyncio here, lazily, right before use.
    import asyncio

    async def _run_all() -> list[FaultTrajectory]:
        return [await run_live(args.engine, args.kill_point) for _ in range(args.n)]

    trajectories = asyncio.run(_run_all())
    for t in trajectories:
        print(t)
    return 0


if __name__ == "__main__":
    sys.exit(main())
