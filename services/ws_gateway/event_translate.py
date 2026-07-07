from __future__ import annotations


def translate_event(raw: dict, *, turn_id: str) -> dict | None:
    """Translate an orchestrator snake_case event into a frontend StreamEvent.

    Returns None for events the frontend stream contract does not include
    (e.g. answer.done — the frontend assembles the answer from answer.delta).
    """
    etype = raw.get("type")

    if etype == "turn.start":
        return {
            "type": "node.enter",
            "turnId": turn_id,
            "node": raw.get("node", "plan_node"),
            "thinkingBudget": raw.get("thinking_budget", 0),
        }

    if etype == "reasoning":
        return {"type": "reasoning.delta", "turnId": turn_id, "text": raw.get("text", "")}

    if etype == "tool.start":
        return {
            "type": "tool.start",
            "turnId": turn_id,
            "toolCall": {
                "id": raw.get("tool_id", ""),
                "name": raw.get("name", "tool"),
                "kind": raw.get("kind", "tool"),
                "summary": raw.get("summary", ""),
                "reasoningWhy": raw.get("reasoning_why", ""),
                "args": raw.get("args", {}),
            },
        }

    if etype == "tool.done":
        return {
            "type": "tool.done",
            "turnId": turn_id,
            "toolId": raw.get("tool_id", ""),
            "status": raw.get("status", "done"),
            "summary": raw.get("summary", ""),
            "result": raw.get("result"),
            "durationMs": raw.get("duration_ms", 0),
        }

    if etype == "answer.delta":
        return {"type": "answer.delta", "turnId": turn_id, "text": raw.get("text", "")}

    if etype == "turn.done":
        return {"type": "turn.done", "turnId": turn_id, "status": raw.get("status", "complete")}

    if etype == "context":
        return {"type": "context.update", "window": raw.get("window", {})}

    if etype == "agent_status":
        return {"type": "agent.status", "status": raw.get("status", {})}

    if etype == "artifact_created":
        return {
            "type": "artifact.created",
            "turnId": turn_id,
            "artifact": raw.get("artifact", {}),
        }

    return None
