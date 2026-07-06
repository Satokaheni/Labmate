"""Plain-async orchestrator reproducing the LangGraph graph's SINGLE-goal path
(strangler, flag-gated behind ORCHESTRATOR_ENGINE=lite). Reuses
AsyncOrchestrator.react_execute for execution."""

from __future__ import annotations

import json

from . import client_context, events, lite_persistence
from .graph import AMBIGUITY_THRESHOLD, ASSESS_MAX_TOKENS, ASSESS_THINKING_BUDGET, MAX_GOAL_ATTEMPTS
from .lite_approval import requires_approval
from .lite_state import build_initial_state
from .task_complexity import classify_complexity


async def run_goal_lite(
    orch,
    async_orch,
    task: str,
    session_id: str,
    user_id: str = "",
    workspace_id: str = "",
    store=None,
    signals=None,
) -> dict:
    """Plain-async reproduction of the LangGraph `assess_ambiguity` node +
    `ambiguity_router` (graph.py:591-731, 915-924). No graph, no checkpointer —
    just the single-goal state dict threaded through in-process."""
    state = build_initial_state(task, session_id, user_id=user_id, workspace_id=workspace_id)

    goal = state.get("root_goal") or state["goal_tree"][state["current_goal_id"]]["description"]
    # When a local workspace client is attached, the workspace IS the target —
    # "which codebase/repo?" is never a valid clarification. Without this the
    # ambiguity triage asks the user where to look instead of searching it.
    workspace_note = (
        "CONTEXT: A local workspace IS attached and IS the target of this task. "
        "Treat the attached workspace as the codebase/project by default. A missing "
        "repository / codebase / project / folder name is NOT ambiguity, and you "
        "MUST NEVER set blocking_question to ask which codebase/repo/folder to use — "
        "the agent searches the attached workspace. Only WHAT to do can be ambiguous "
        "here, never WHERE the code lives.\n\n"
        if client_context.get_manifest() is not None
        else ""
    )
    # Prepend prior conversation so the ambiguity triage can RESOLVE referents
    # ("that problem" -> the thing discussed last turn) instead of flagging them as
    # undefined.
    continuity_block = ""
    _cm = getattr(orch, "context_manager", None)
    if _cm is not None:
        try:
            continuity_block = await _cm.conversation_context(state.get("session_id", ""))
        except Exception:
            continuity_block = ""
    prior_context = (
        "PRIOR CONVERSATION (use this to resolve referents like 'that'/'it'/'this'; a "
        "referent RESOLVABLE from this history is NOT ambiguity — score it LOW):\n"
        f"{continuity_block}\n\n"
        if continuity_block
        else ""
    )
    prompt = (
        "You are triaging a task before an autonomous agent executes it.\n"
        f"{prior_context}"
        f"TASK: {goal}\n\n"
        f"{workspace_note}"
        "List the assumptions an agent must make to act on this as written. "
        "Then rate overall ambiguity from 0.0 (fully specified) to 1.0 (critically "
        "underspecified).\n\n"
        "The score measures ONE thing only: is the CORE objective underspecified? "
        "Score it on whether the agent can tell WHAT to do, not on whether every "
        "execution detail is pinned down.\n\n"
        "Score HIGH (>= 0.6, typically 0.7-0.9) ONLY when the CORE task is "
        "underspecified, i.e. it has any of:\n"
        '  - an undefined referent (e.g. "it", "the thing", "this") with no '
        "antecedent naming the actual object,\n"
        "  - no concrete deliverable (you cannot tell what artifact to produce),\n"
        "  - undefined success criteria (you'd have to guess what 'done' means, or "
        "no target/metric is given for a 'make it better'-style request),\n"
        "  - essential information missing without which you cannot meaningfully "
        "begin.\n\n"
        "Score LOW (~0.0-0.3) when the core objective is clear and actionable, EVEN "
        "IF minor execution parameters are unstated. Unstated MINOR parameters are "
        "NOT ambiguity — the agent should assume reasonable defaults and proceed. "
        "These do NOT raise the score:\n"
        "  - output format (list vs table vs JSON),\n"
        "  - count or quantity (how many results/items),\n"
        "  - which specific library, method, or algorithm to use,\n"
        "  - styling, naming, or other cosmetic choices.\n\n"
        "Examples:\n"
        '  "make it better" -> 0.85  (undefined referent, no deliverable)\n'
        '  "fix the thing" -> 0.9  (undefined referent, no deliverable)\n'
        '  "improve performance" (no system/target/metric) -> 0.75  (undefined '
        "success criteria)\n"
        '  "search the Hugging Face Hub for emotion classification datasets" -> 0.1 '
        " (clear objective; format and count are defaults, not ambiguity)\n"
        '  "write a python function that reverses a string" -> 0.1  (the '
        "implementation method is just a default to pick)\n"
        '  "what is 2+2?" -> 0.0\n'
        '  "add a docstring to the reverse_string function in utils.py" -> 0.1\n\n'
        'When ambiguity is high, set "blocking_question" to the single most useful '
        "question to ask the user; otherwise leave it empty.\n"
        "Respond as JSON: "
        '{"assumptions": ["..."], "ambiguity": 0.0, "blocking_question": "" }'
    )
    raw = await orch.architect(
        prompt, thinking_budget=ASSESS_THINKING_BUDGET, max_tokens=ASSESS_MAX_TOKENS
    )
    text = (raw or "").strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    try:
        out = json.loads(text.strip())
        if not isinstance(out, dict):
            raise ValueError("not an object")
    except (json.JSONDecodeError, ValueError):
        out = {}
    try:
        ambiguity = float(out.get("ambiguity", 0.0))
    except (TypeError, ValueError):
        ambiguity = 0.0

    assumptions = out.get("assumptions", []) or []
    blocking_question = out.get("blocking_question", "") or ""
    # Human-readable reasoning text for connectors (the CLI renders this verbatim):
    # the blocking question when ambiguous, else a short assumptions line — never raw JSON.
    if blocking_question:
        reasoning_text = blocking_question
    elif assumptions:
        reasoning_text = "Assuming: " + "; ".join(str(a) for a in assumptions[:3])
    else:
        reasoning_text = ""
    await events.emit(
        "reasoning",
        node="assess_ambiguity",
        summary=f"ambiguity={ambiguity:.2f}; {len(assumptions)} assumption(s)",
        text=reasoning_text,
    )

    state["root_goal"] = goal
    state["assumptions"] = assumptions
    state["ambiguity"] = ambiguity
    state["blocking_question"] = blocking_question

    # Task 3: complexity classifier determines the skip flags (deterministic, pure).
    complexity = classify_complexity(goal)
    state["complexity"] = {
        "skip_ambiguity": complexity.skip_ambiguity,
        "skip_verify": complexity.skip_verify,
        "reason": complexity.reason,
    }
    state["skip_ambiguity"] = complexity.skip_ambiguity
    state["skip_verify"] = complexity.skip_verify

    # ambiguity_router (graph.py:915-924): skip_ambiguity short-circuits straight to
    # plan; otherwise halt (END) on high ambiguity instead of guessing.
    if state.get("skip_ambiguity"):
        pass  # proceed
    elif ambiguity >= AMBIGUITY_THRESHOLD:
        question = blocking_question or "Could you clarify what you'd like me to do?"
        await events.emit(
            "clarification_request",
            question=question,
            task=goal,
            session_id=state.get("session_id", ""),
        )
        state["awaiting_clarification"] = True
        state["clarification_question"] = question
        state["final_answer"] = question
        return state

    # NOTE: direct-answer fast-path deferred; react_execute handles trivial tasks
    # execute_node (graph.py:300) ultimately drives react_execute; reuse it (do NOT
    # fork the tool loop). Single-intent: the goal itself is the intent.

    # Approval gate (reproduces approval:572). Only gated when a SignalRegistry is
    # supplied — callers that don't wire signals (e.g. unit tests exercising only
    # the ambiguity/execute path) get the pre-Task-6 behavior of always executing.
    if signals is not None and requires_approval(task):
        await events.emit(
            "reasoning",
            node="approval",
            summary="awaiting approval",
            text="This task requests an irreversible action; awaiting approval.",
        )
        if store is not None:
            await lite_persistence.save_suspend(store, session_id, state, phase="await_approval")
        decision = await signals.await_approval(session_id)
        if store is not None:
            await lite_persistence.clear(store, session_id)
        if decision == "reject":
            state["final_answer"] = "Blocked — the irreversible action was not approved."
            state["ok"] = False
            return state
        # approve -> fall through to execute

    # NOTE: verify gate is a no-op in default config (critique off); deferred.

    # Reflect-retry loop (reproduces check/router's FAILED-and-attempts<MAX_GOAL_ATTEMPTS
    # -> reflect -> execute, graph.py:883-907, and reflect:526's diagnosis).
    goal = task
    result: dict = {}
    for attempt in range(MAX_GOAL_ATTEMPTS):
        result = await async_orch.react_execute(goal)
        if result.get("ok"):
            break
        if attempt + 1 < MAX_GOAL_ATTEMPTS:
            # reflect (graph.py:526): a bounded diagnosis pass that informs the retry
            diag = await orch.architect(
                f"The last attempt at this task failed. Summary: {result.get('summary', '')}. "
                "Diagnose the root cause in 1-2 sentences and state what to do differently."
            )
            goal = f"{task}\n\nA prior attempt FAILED. Diagnosis: {diag}\nApply this and try again."

    state["final_answer"] = result.get("summary", "")
    state["ok"] = result.get("ok", False)
    state["tests_passed"] = result.get("tests_passed", False)
    state["_result"] = result
    return state
