# AGENTS.md Design — 2026-07-02

## What this covers

How Labmate discovers, loads, and applies project-level AGENTS.md instructions to
every execution path: both the architect (`_build_messages`) and the ReAct/edit loop
(`_run_react_loop`).

---

## Discovery & loading

`workspace_manager.load_agent_instructions()` reads from the **workspace root only**
(no recursive/nested search). This matches both reference harnesses:
- hermes-agent: root-only (`<workspace>/AGENTS.md`)
- openclaw: root-only (`<workspace>/AGENTS.md`)

Precedence: `AGENTS.md` wins over legacy `AGENT.md` (fallback). Content is capped at
`AGENT_INSTRUCTIONS_MAX_CHARS=16000` characters. Loaded once per task in `main.py`
and stored on `CodingOrchestrator.agent_instructions`.

---

## Injection points

### 1. Architect path (`_build_messages`)
Already present before this change. A `{"role": "system", "content": agent_instructions}`
message is appended to the messages list when `self.agent_instructions` is non-empty.

### 2. ReAct/edit loop (`_run_react_loop`) — **gap closed by this change**
`agent_instructions` is now passed to `PromptAssembler(agent_instructions=...)` and
appended to `system_text` as a labeled section:

```
# Project instructions (AGENTS.md)

<content of AGENTS.md>
```

This is the missing piece: when the agent is actually reading/editing/running tools it
now sees the same project instructions the architect sees.

---

## Placement in the cached prefix

`agent_instructions` is **session/goal-stable** (read once per task, never changes
mid-loop). It therefore belongs in the **byte-stable prefix** that `PromptAssembler`
freezes at goal start — not in the dynamic per-turn tail.

This mirrors hermes-agent's approach (project instructions in the frozen system
prefix) rather than openclaw's (injected below the cache boundary). Placing it in
the prefix means llama.cpp's longest-common-prefix cache still hits on every
subsequent ReAct step within the same goal.

**Byte-identical when empty:** when `agent_instructions` is absent or
whitespace-only, the assembled `system_text` is byte-identical to the pre-change
output. No header is added, no trailing newline. A workspace without AGENTS.md
produces an identical prefix fingerprint to before this change.

---

## Cache safety proof

- `canonical_prefix()` serializes `system_text` (which now includes the
  `agent_instructions` section when non-empty) + `tools` with `sort_keys=True`.
- `prefix_fingerprint()` is SHA-256 of `canonical_prefix()`.
- Two assemblers built with the same `agent_instructions` produce identical
  fingerprints (cache hit). Assemblers with different values produce different
  fingerprints (correct isolation). Empty == no-arg (backward compat).

---

## Explicit non-decisions

- **Nested AGENTS.md**: not implemented. Both reference harnesses are root-only;
  nested support adds complexity with unclear benefit.
- **Persistent auth in AGENTS.md**: explicitly split off and parked. Credentials
  must never be stored in a project file read into the model context.
- **Threat-scanning**: not applied. Labmate is a single-user system; the workspace
  root is controlled by the operator.
