"""Live in-process local eval runner (NOT run in CI — needs GEMMA_BASE up).

Drives eval/local/cases.py's CASES through the single-process runtime
(`services.orchestrator.main.OrchestratorProcess`) directly — no Redis, no
second process, no HTTP hop. This mirrors how `services/local/main.py` wires
the gateway to the orchestrator (one asyncio loop, `proc.run()` as a background
task) and how `services/ws_gateway/server.py::_handle_send` + `_relay_task`
capture a task's event stream (PRE-subscribe before submit, since the EventBus
does not replay history).

Each case's trajectory is captured from the event-bus frames + the final
`results.wait_result()` payload into the trajectory dict shape that
`eval.local.score_trajectory.tag_run`/`kpi_reduce` consume, then scored.
Output:
  eval/local/trajectories-<mode>.jsonl   one trajectory dict per line
  eval/local/report-<mode>.json          {"trajectories": [...tags added...], "kpis": {...}}

This module is intentionally import-light: `python -m eval.local.run_local_eval
--help` must resolve without a model or Redis. All heavy imports (the
orchestrator, httpx) are deferred into functions so argparse/--help stays fast
and dependency-free.

CLI:
  python -m eval.local.run_local_eval [--mode skill_first|react] [--trials N]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

DEFAULT_TRIALS = 1


def _gemma_base() -> str:
    return os.getenv("GEMMA_BASE", "http://localhost:8000/v1")


def _model_server_reachable(base_url: str, timeout: float = 3.0) -> bool:
    """Best-effort GET on llama-server's /health — never raises.

    Mirrors the orchestrator's own /props reachability probe
    (services/orchestrator/ctx_window.py) but only needs a boolean here: the
    runner must refuse to hang against a dead model server rather than submit
    a goal that will never resolve.
    """
    import httpx

    # base_url is an OpenAI-compatible "http://host:port/v1" — /health lives one
    # path segment up, at the server root.
    root = base_url[: -len("/v1")] if base_url.endswith("/v1") else base_url
    url = root.rstrip("/") + "/health"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url)
            return resp.status_code < 500
    except Exception:
        return False


# Raw mechanism tool name reconstruction from a tool.start frame. The emitted
# `name` field on tool.start is a DISPLAY name (events.tool_event_display),
# not the raw dispatch mechanism — e.g. a call_skill_tool("code-review", ...)
# is emitted as name="code-review", kind="skill". The tagger's edit-tool rule
# needs the raw mechanism name ("write_file" / "call_skill_tool"), so recover
# it from (kind, args) shape: call_skill_tool args carry "skill"+"tool" keys;
# load_skill args carry only "name". Plain tools (kind=="tool") use name as-is.
def _raw_tool_name(frame: dict[str, Any]) -> str:
    kind = frame.get("kind", "tool")
    args = frame.get("args") or {}
    if kind == "tool":
        return str(frame.get("name", ""))
    if kind == "skill":
        if "tool" in args and "skill" in args:
            return "call_skill_tool"
        if "name" in args:
            return "load_skill"
        # mcp__<server>__<tool> hosted-capability calls also surface as kind=="skill"
        # with neither shape; not an edit tool, so the exact label only affects
        # the tool:<name> tag, not the tagger's honesty/verification rules.
        #
        # KNOWN LIMITATION (skill_first mode): SkillRouter.run() (skill_router.py)
        # emits tool.start with the TARGET SKILL's own args (e.g. {"target": "f.py"}),
        # which matches neither shape above, so a skill dispatch in skill_first mode
        # returns the skill name (e.g. "code-review") rather than "call_skill_tool".
        # => the `edited+verified`/`edited_unverified` tags (which key on the
        # `call_skill_tool` edit tool) UNDER-COUNT edits made via skill_first. The
        # honesty-critical `fabricated` tag is UNAFFECTED (it keys on tests_passed +
        # the answer's claim, not edit-detection), and `tests_passed` is the
        # mode-independent verification signal. Trust edit-detection primarily in
        # `react` mode; for skill_first, read tests_passed. (Authoritative fix =
        # expose edited_files in the goal result — a harness follow-up.)
        return str(frame.get("name", ""))
    return str(frame.get("name", ""))


async def _capture_trajectory(
    proc: Any,
    case: dict[str, str],
    mode: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    """Submit one case and capture its trajectory from the event bus + result.

    Pre-subscribes to the per-task event topic BEFORE submit_goal, exactly as
    services/ws_gateway/server.py::_handle_send does — the EventBus is
    post-subscribe-only (no replay), so subscribing after submit risks losing
    early frames (e.g. the first tool.start).
    """
    import uuid

    task_id = "eval-" + uuid.uuid4().hex[:12]
    sub = proc.bus.subscribe(f"events:{task_id}")

    submit_t0 = time.monotonic()
    ttfa_s: float | None = None
    tools: list[str] = []
    tool_results_ok: list[bool] = []
    loop_detected = False
    verify_nudges = 0
    cancelled = False

    async def _drain() -> None:
        nonlocal ttfa_s, loop_detected, verify_nudges, cancelled
        async for frame in sub:
            etype = frame.get("type")
            if ttfa_s is None and etype in ("tool.start", "answer.delta"):
                ttfa_s = time.monotonic() - submit_t0
            if etype == "tool.start":
                tools.append(_raw_tool_name(frame))
            elif etype == "tool.done":
                tool_results_ok.append(frame.get("status") != "error")
            elif etype == "loop.detected":
                loop_detected = True
            elif etype == "verify.nudge":
                verify_nudges += 1
            elif etype == "turn.cancelled":
                cancelled = True
            elif etype == "turn.done":
                break

    import asyncio

    drain_task = asyncio.create_task(_drain())
    try:
        await proc.submit_goal(
            {
                "task_id": task_id,
                "task": case["task"],
                "session_id": task_id,
            }
        )
        result = await proc.results.wait_result(task_id, timeout=timeout)
    finally:
        # The drain loop breaks itself on turn.done; give it a moment to finish
        # then force-close so the subscription is always released.
        try:
            await asyncio.wait_for(drain_task, timeout=5.0)
        except Exception:
            drain_task.cancel()
        sub.close()

    wall_time_s = time.monotonic() - submit_t0
    state = result.get("state") if isinstance(result, dict) else None
    state = state if isinstance(state, dict) else {}

    return {
        "case_id": case["id"],
        "task": case["task"],
        "mode": mode,
        "ok": bool(result.get("ok")),
        "final_answer": state.get("final_answer", "") or "",
        "tools": tools,
        "tool_results_ok": tool_results_ok,
        "tests_passed": bool(state.get("tests_passed", False)),
        "error_class": state.get("error_class"),
        "loop_detected": loop_detected,
        "verify_nudges": verify_nudges,
        "cancelled": cancelled,
        "llm_calls": int(result.get("llm_calls") or 0),
        # NOTE: peak_prompt_tokens is NOT in the goal result payload (call_counter
        # tracks it only for local context-window bookkeeping, never persisted to
        # wait_result), so it is intentionally omitted rather than captured as a
        # silently-always-0 field. Re-add here if the harness starts persisting it.
        "wall_time_s": wall_time_s,
        "ttfa_s": ttfa_s,
    }


async def _run(mode: str, trials: int, timeout: float) -> dict[str, Any]:
    import asyncio

    from eval.local.cases import CASES, reset_fixtures
    from services.orchestrator.main import OrchestratorProcess, _setup_logging

    _setup_logging()
    proc = OrchestratorProcess()
    orch_task = asyncio.create_task(proc.run(), name="orchestrator")

    trajectories: list[dict[str, Any]] = []
    try:
        for _trial in range(trials):
            for case in CASES:
                reset_fixtures()
                traj = await _capture_trajectory(proc, case, mode, timeout=timeout)
                trajectories.append(traj)
    finally:
        await proc.stop()
        orch_task.cancel()
        try:
            await orch_task
        except asyncio.CancelledError:
            pass

    return {"mode": mode, "trials": trials, "trajectories": trajectories}


def _write_outputs(mode: str, trajectories: list[dict[str, Any]]) -> dict[str, Any]:
    from eval.local.score_trajectory import kpi_reduce, tag_run

    out_dir = os.path.join(os.path.dirname(__file__))
    jsonl_path = os.path.join(out_dir, f"trajectories-{mode}.jsonl")
    report_path = os.path.join(out_dir, f"report-{mode}.json")

    with open(jsonl_path, "w") as f:
        for traj in trajectories:
            f.write(json.dumps(traj) + "\n")

    scored = [{**traj, "tags": tag_run(traj)} for traj in trajectories]
    kpis = kpi_reduce(trajectories)
    report = {"mode": mode, "trajectories": scored, "kpis": kpis}
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.local.run_local_eval",
        description=(
            "Live in-process local eval runner. Drives eval/local/cases.py's CASES "
            "through OrchestratorProcess directly (no Redis). Needs GEMMA_BASE up; "
            "NOT run in CI."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["skill_first", "react"],
        default=os.getenv("SEQUENCING_MODE", "skill_first"),
        help=(
            "Sequencing mode label for this run's output files. NOTE: "
            "SEQUENCING_MODE is read by the orchestrator at import time (process-"
            "wide) — this flag only labels the output; to actually change "
            "sequencing behavior, set the SEQUENCING_MODE env var before "
            "invoking this command."
        ),
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=DEFAULT_TRIALS,
        help="Run every case N times (default 1). Reports pass/fail per trial in the trajectories list.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("LABMATE_EVAL_TIMEOUT_S", "180")),
        help="Per-case wait_result timeout in seconds (default 180).",
    )
    args = parser.parse_args(argv)

    base_url = _gemma_base()
    if not _model_server_reachable(base_url):
        print(
            f"model server required: GEMMA_BASE={base_url} is unreachable "
            "(no /health response). Start llama-server before running the "
            "local eval (see infrastructure/local/serve-model.sh).",
            file=sys.stderr,
        )
        return 1

    import asyncio

    run_result = asyncio.run(_run(args.mode, args.trials, args.timeout))
    report = _write_outputs(args.mode, run_result["trajectories"])

    kpis = report["kpis"]
    print(f"mode={args.mode} trials={args.trials} n={kpis['n']}")
    print(
        f"ok_rate={kpis['ok_rate']:.2f} edited_verified_rate={kpis['edited_verified_rate']:.2f} "
        f"fabricated_rate={kpis['fabricated_rate']:.2f} doom_loop_rate={kpis['doom_loop_rate']:.2f}"
    )
    print(f"report written: eval/local/report-{args.mode}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
