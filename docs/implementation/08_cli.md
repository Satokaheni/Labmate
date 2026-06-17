# Implementation Plan: CLI Interface

The CLI is the primary way to test and use Labmate once Milestone 3+ is built. It replaces `main.py` (M2 prototype) with a terminal loop wired to the real M3+ orchestrator — streaming tokens, showing tool calls, and persisting sessions.

**Entry point:** `labmate.py` at the project root.

---

## 1. What This Does

A terminal chat loop that:
- Sends user input to the M3+ LangGraph orchestrator
- Streams LLM tokens to the terminal as they arrive (no waiting for the full response)
- Shows tool calls being dispatched and their results inline
- Persists sessions to MongoDB so you can resume previous conversations
- Replaces `main.py` entirely once the orchestrator is stable

It does NOT load the model — it talks to the running vLLM + orchestrator stack, the same as any other client would.

---

## 2. Dependencies

Must be running before starting the CLI:
- vLLM on host (`http://localhost:8000/health` → 200)
- `lm-mongodb`, `lm-chroma`, `lm-redis` containers
- `lm-mcp-bridge` container (or the orchestrator connects to it directly)

Python packages (add to `services/orchestrator/requirements.txt`):
```
rich>=13.7          # terminal formatting, streaming output
prompt_toolkit>=3.0 # multi-line input, history, key bindings
```

---

## 3. File Structure

```
labmate/                           ← project root
├── labmate.py                     ← CLI entry point (replaces main.py)
├── cli/
│   ├── __init__.py
│   ├── display.py                 ← Rich console output (streaming, tool calls, status)
│   ├── session.py                 ← Session selection and resume logic
│   └── keybindings.py             ← prompt_toolkit key bindings (Ctrl+C, multi-line)
└── main.py                        ← M2 prototype — DO NOT MODIFY, keep as fallback
```

The CLI imports the M3+ orchestrator directly:
```python
from services.orchestrator.orchestrator import CodingOrchestrator
```

So `labmate.py` must be run from the project root with the `services/` directory on the Python path.

---

## 4. Interface Contracts

The CLI drives the orchestrator via its public Python API — no HTTP, no MCP. It calls the orchestrator directly in the same process.

### Orchestrator API the CLI uses

```python
class CodingOrchestrator:
    async def start_session(self, goal: str) -> str:
        """Create a new session, return session_id."""

    async def resume_session(self, session_id: str) -> None:
        """Load an existing session from MongoDB."""

    async def stream(
        self,
        user_input: str,
        on_token: Callable[[str], None],
        on_tool_call: Callable[[str, dict], None],
        on_tool_result: Callable[[str, str], None],
    ) -> str:
        """
        Run one turn. Calls callbacks as events arrive:
          on_token(text)              — LLM token to display
          on_tool_call(name, args)    — tool being dispatched
          on_tool_result(name, text)  — result returned
        Returns the full assistant response.
        """

    @property
    def session_id(self) -> str: ...

    @property
    def goal_tree(self) -> dict: ...
```

### Session resume format (from MongoDB)

```python
# List recent sessions for the resume prompt:
sessions = await storage.list_sessions(limit=10)
# Returns: [{"session_id": "...", "goal": "...", "created_at": "...", "status": "..."}]
```

---

## 5. Implementation Steps

### Step 1 — `cli/display.py` (Rich output)

Create a `Display` class wrapping a `rich.console.Console`. All terminal output goes through this — never `print()` in `labmate.py`.

```python
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

class Display:
    def __init__(self):
        self.console = Console()
        self._live: Live | None = None
        self._buffer = ""

    def start_stream(self):
        """Begin a streaming response — enters Rich Live context."""
        self._buffer = ""
        self._live = Live(console=self.console, refresh_per_second=15)
        self._live.__enter__()

    def append_token(self, token: str):
        """Add a token to the streaming buffer and refresh."""
        self._buffer += token
        self._live.update(Markdown(self._buffer))

    def end_stream(self):
        """Finish streaming — renders final markdown."""
        self._live.__exit__(None, None, None)
        self._live = None

    def tool_call(self, name: str, args: dict):
        """Show a tool being dispatched."""
        self.console.print(f"\n  [dim]→ tool:[/dim] [cyan]{name}[/cyan] [dim]{args}[/dim]")

    def tool_result(self, name: str, result: str):
        """Show a tool result (truncated)."""
        preview = result[:120] + "…" if len(result) > 120 else result
        self.console.print(f"  [dim]← {name}:[/dim] [green]{preview}[/green]\n")

    def error(self, msg: str):
        self.console.print(f"[red]Error:[/red] {msg}")

    def info(self, msg: str):
        self.console.print(f"[dim]{msg}[/dim]")

    def header(self):
        self.console.print(Panel(
            "[bold cyan]Labmate[/bold cyan] — autonomous coding & writing agent\n"
            "[dim]Type your task. Ctrl+D to exit. /new for a new session. /resume to continue a previous one.[/dim]",
            expand=False
        ))
```

### Step 2 — `cli/session.py` (session management)

```python
async def pick_session(storage: StorageManager, display: Display) -> str | None:
    """
    Show recent sessions and let the user pick one to resume, or start new.
    Returns session_id to resume, or None to start a new session.
    """
    sessions = await storage.list_sessions(limit=10)
    if not sessions:
        return None

    display.console.print("\n[bold]Recent sessions:[/bold]")
    for i, s in enumerate(sessions, 1):
        ts = s["created_at"][:10]
        goal_preview = s["goal"][:60] + "…" if len(s["goal"]) > 60 else s["goal"]
        status_color = "green" if s["status"] == "completed" else "yellow"
        display.console.print(
            f"  [{i}] [{status_color}]{s['status']}[/{status_color}] "
            f"[dim]{ts}[/dim] — {goal_preview}"
        )

    display.console.print("  [0] Start a new session")
    choice = input("\nPick a session (or press Enter for new): ").strip()

    if not choice or choice == "0":
        return None
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(sessions):
            return sessions[idx]["session_id"]
    except ValueError:
        pass
    return None
```

### Step 3 — `cli/keybindings.py` (prompt_toolkit setup)

```python
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from pathlib import Path

def make_prompt_session() -> PromptSession:
    history_file = Path.home() / ".labmate_history"
    bindings = KeyBindings()

    # Ctrl+J or Alt+Enter for newline in input (Enter submits)
    @bindings.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    return PromptSession(
        history=FileHistory(str(history_file)),
        key_bindings=bindings,
        multiline=False,
    )
```

### Step 4 — `labmate.py` (main entry point)

```python
#!/usr/bin/env python3
"""
Labmate CLI — replaces main.py once M3+ orchestrator is running.

Usage:
    python labmate.py              # start or resume a session
    python labmate.py --new        # always start a new session
    python labmate.py --session <id>  # resume a specific session
"""
import asyncio
import argparse
import sys
from pathlib import Path

# Make services/ importable from project root
sys.path.insert(0, str(Path(__file__).parent))

from services.orchestrator.orchestrator import CodingOrchestrator
from services.orchestrator.memory.storage import StorageManager
from cli.display import Display
from cli.session import pick_session
from cli.keybindings import make_prompt_session


async def main(args: argparse.Namespace) -> None:
    display = Display()
    display.header()

    storage = StorageManager()
    await storage.connect()

    orchestrator = CodingOrchestrator()

    # ── Session selection ──────────────────────────────────────────────────────
    session_id = None
    if args.session:
        session_id = args.session
    elif not args.new:
        session_id = await pick_session(storage, display)

    if session_id:
        await orchestrator.resume_session(session_id)
        display.info(f"Resumed session {session_id[:8]}…")
    else:
        display.info("Starting new session. Type your first task.")

    # ── Chat loop ──────────────────────────────────────────────────────────────
    prompt = make_prompt_session()

    while True:
        try:
            user_input = await asyncio.get_event_loop().run_in_executor(
                None, lambda: prompt.prompt("You: ")
            )
        except (EOFError, KeyboardInterrupt):
            display.info("\nGoodbye.")
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        # ── Slash commands ─────────────────────────────────────────────────────
        if user_input == "/new":
            session_id = None
            display.info("Starting a new session.")
            continue
        if user_input == "/resume":
            session_id = await pick_session(storage, display)
            if session_id:
                await orchestrator.resume_session(session_id)
                display.info(f"Resumed session {session_id[:8]}…")
            continue
        if user_input == "/session":
            display.info(f"Current session: {orchestrator.session_id or 'none'}")
            continue
        if user_input.startswith("/help"):
            display.console.print(
                "\n  [bold]Commands:[/bold]\n"
                "  /new       — start a new session\n"
                "  /resume    — pick a previous session to continue\n"
                "  /session   — show current session ID\n"
                "  /goals     — show goal tree for current session\n"
                "  Ctrl+D     — exit\n"
            )
            continue
        if user_input == "/goals":
            import json
            display.console.print_json(json.dumps(orchestrator.goal_tree, indent=2))
            continue

        # ── New session if none active ─────────────────────────────────────────
        if not orchestrator.session_id:
            await orchestrator.start_session(user_input)

        # ── Stream the response ────────────────────────────────────────────────
        display.start_stream()
        try:
            await orchestrator.stream(
                user_input=user_input,
                on_token=display.append_token,
                on_tool_call=lambda name, args: (
                    display.end_stream(),
                    display.tool_call(name, args),
                    display.start_stream(),
                ),
                on_tool_result=lambda name, result: (
                    display.end_stream(),
                    display.tool_result(name, result),
                    display.start_stream(),
                ),
            )
        except Exception as e:
            display.end_stream()
            display.error(str(e))
            continue
        finally:
            if display._live:
                display.end_stream()

        display.console.print()  # blank line after response


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Labmate — autonomous coding & writing agent")
    p.add_argument("--new", action="store_true", help="Always start a new session")
    p.add_argument("--session", metavar="ID", help="Resume a specific session by ID")
    return p.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
```

### Step 5 — Wire `stream()` callbacks into the orchestrator

In `services/orchestrator/orchestrator.py`, the `stream()` method needs to accept and call the three callbacks:

```python
async def stream(
    self,
    user_input: str,
    on_token: Callable[[str], None] | None = None,
    on_tool_call: Callable[[str, dict], None] | None = None,
    on_tool_result: Callable[[str, str], None] | None = None,
) -> str:
    # Append user message to history
    await self.storage.write_message(self.session_id, "user", user_input)

    full_response = ""
    async for event in self._run_graph(user_input):
        if event["type"] == "token":
            full_response += event["text"]
            if on_token:
                on_token(event["text"])
        elif event["type"] == "tool_call":
            if on_tool_call:
                on_tool_call(event["name"], event["args"])
        elif event["type"] == "tool_result":
            if on_tool_result:
                on_tool_result(event["name"], event["text"])

    await self.storage.write_message(self.session_id, "assistant", full_response)
    return full_response
```

The `_run_graph()` async generator yields typed event dicts from the LangGraph execution.

### Step 6 — Smoke test

```bash
# Verify the stack is up
./infrastructure/docker/scripts/run-services.sh --infra-only
curl http://localhost:8000/health  # vLLM must be running on host

# Run the CLI
python labmate.py --new

# Expected: header prints, "You: " prompt appears
# Type: "what is 2 + 2"
# Expected: streaming response appears token by token
# Type: "list files in /tmp"
# Expected: tool call line appears, then result, then response
```

---

## 6. Key Behaviors

| Command / Input | What happens |
|----------------|--------------|
| `You: <task>` | Streams response, shows tool calls inline |
| `/new` | Clears session, next input starts a new one |
| `/resume` | Lists recent sessions, lets you pick one |
| `/session` | Prints current session ID (for debugging) |
| `/goals` | Prints the goal tree JSON for the current session |
| `/help` | Lists commands |
| Ctrl+D | Clean exit |
| Ctrl+C mid-response | Cancels the current turn (orchestrator stays alive) |

---

## 7. Integration Verification

```bash
# 1. Basic turn — no tools
python labmate.py --new
You: say hello
# Expected: streaming "Hello! ..." token by token

# 2. Tool call turn
You: list files in /workspace
# Expected:
#   → tool: repo_map {'path': '/workspace'}
#   ← repo_map: {"symbols": [...]}
#   <streaming response>

# 3. Session persistence
python labmate.py --new
You: my name is Zach
# Note the session ID printed in /session
python labmate.py --session <id>
You: what's my name?
# Expected: remembers "Zach" from previous turn

# 4. Resume prompt
python labmate.py
# Expected: lists recent sessions, lets you pick one
```

---

## 8. Done Criteria

- [ ] `python labmate.py` starts without errors (stack must be running)
- [ ] First input creates a session visible in MongoDB `sessions` collection
- [ ] LLM tokens stream to terminal as they arrive (no buffering until completion)
- [ ] Tool calls appear as `→ tool: name {args}` inline during streaming
- [ ] Tool results appear as `← name: preview` before streaming resumes
- [ ] `/resume` lists sessions from MongoDB and resumes the selected one
- [ ] Session context persists: a fact mentioned in turn 1 is recalled in turn 3
- [ ] Ctrl+D exits cleanly
- [ ] `python labmate.py --new` always starts fresh regardless of existing sessions

---

## 9. Transition from M2

When the M3+ orchestrator is stable:

1. Verify `python labmate.py` handles the same inputs that `python main.py` handled
2. Run both side by side for a session to compare output quality
3. Once satisfied, update `README.md` quick start to use `labmate.py` instead of `main.py`
4. `main.py` stays on disk as a fallback — do not delete it

---

## 10. Future: REST API + Web Interface

When you're ready to add a web interface, the same `orchestrator.stream()` method works — swap `on_token` from a terminal print to a WebSocket send or SSE write. The CLI proves the orchestrator's public API is stable before the web layer is built on top of it.

Rough shape of a future REST endpoint:
```
POST /sessions          → create session, return session_id
POST /sessions/{id}/chat (SSE) → stream a turn, events as SSE chunks
GET  /sessions          → list sessions
GET  /sessions/{id}     → get session + goal tree
DELETE /sessions/{id}   → cancel
```
The design (auth, web UI framework, deployment) is deferred until Labmate is mature.
