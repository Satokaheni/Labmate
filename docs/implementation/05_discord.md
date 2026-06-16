# 05 — Discord Connector Implementation Plan

**Status:** Draft  
**Scope:** `services/orchestrator/discord_connector.py` + `services/orchestrator/main.py`  
**References:** `docs/implementation/00_contracts.md` (Contract F), `research/llm-harness-research/specs/spec_integrations.md`

---

## 1. What This Module Does

The Discord connector is the sole user-facing interface for Labmate. It receives slash commands from Discord users, routes tasks to the LangGraph orchestrator, and streams the orchestrator's token output back to Discord by editing a single message in place — rather than posting one message per chunk.

Concrete responsibilities:

- Expose five slash commands: `/task`, `/status`, `/cancel`, `/approve`, `/reject`
- Acknowledge every slash command interaction within 3 seconds via `defer()` — no exceptions
- Create a dedicated public thread per task (guild mode) to isolate concurrent task output
- Accumulate LLM tokens and edit the in-thread message at most once per second
- Auto-split messages at 1,900 characters, preserving readability by splitting at the last newline
- Guard against Discord's 15-minute interaction token expiry (hard cutoff at 14.5 minutes)
- Fire a Discord Webhook completion notification when each task finishes, fails, or is cancelled
- Bridge human-in-the-loop (HITL) gate approvals from Discord to the orchestrator

The connector is **not** a separate service. It runs in the same process as the orchestrator on the same `asyncio` event loop.

---

## 2. Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `discord.py` | `>=2.4.0` | Gateway client, slash commands (`app_commands`), REST wrappers, `discord.ui` Views |
| `aiohttp` | `>=3.9.0` | Async HTTP; bundled with discord.py; used internally for REST and webhook calls |
| `uvloop` | `>=0.19.0` | Faster event loop (UNIX only; optional but recommended in production) |
| `python-dotenv` | `>=1.0.0` | Environment variable loading |

**No `httpx` needed.** discord.py ships aiohttp and the `discord.Webhook` class covers webhook delivery. If a separate HTTP client is needed elsewhere in the stack, `httpx[asyncio]` is a fine addition, but it is not required by the connector itself.

**Environment variables (Contract F + `00_contracts.md`):**

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_BOT_TOKEN` | yes | Bot token from Discord Developer Portal |
| `DISCORD_WEBHOOK_URL` | no | Webhook URL for completion notifications; omit to disable |
| `DISCORD_DEV_GUILD_ID` | no | Guild ID for instant slash command sync during development |
| `DISCORD_EDIT_INTERVAL` | no | Edit throttle in seconds (default `1.0`) |

**Required Discord bot permissions and intents:**

- Intents: `guilds`, `guild_messages`, `message_content`
- OAuth2 scopes: `bot`, `applications.commands`
- Bot permissions: `Send Messages`, `Create Public Threads`, `Send Messages in Threads`, `Read Message History`, `Use Slash Commands`

---

## 3. File Structure

```
services/orchestrator/
├── discord_connector.py   — DiscordConnector class: all bot logic, slash commands, streaming
├── main.py                — wires bot + orchestrator together via asyncio.gather
└── ...                    — existing orchestrator files (LangGraph, tools, etc.)
```

`discord_connector.py` is self-contained. It imports from the orchestrator by accepting the orchestrator instance in its constructor — no circular imports.

`main.py` is the single entry point. It instantiates both the orchestrator and the connector, then runs them together with `asyncio.gather`.

---

## 4. Interface Contracts

### 4.1 Asyncio Interface: Orchestrator → DiscordConnector

The connector calls the orchestrator via one of two interfaces, detected at runtime:

**Preferred — async generator (streaming):**

```python
async for token in orchestrator.stream(prompt: str) -> AsyncGenerator[str, None]:
    # token is a str chunk (one or more characters; typically a few words)
```

**Fallback — synchronous (blocking):**

```python
result: str = orchestrator.run(prompt: str)
# must be called via loop.run_in_executor, never bare from a coroutine
```

The connector owns token accumulation and Discord delivery. The orchestrator owns token generation. There is no shared queue object — the connector iterates the async generator directly inside `_streaming_worker`.

**HITL gate interface (for `/approve` and `/reject`):**

```python
# Check if a pending gate exists
gate = orchestrator.pending_gate(task_id: str) -> Optional[Any]

# Unblock the gate (resume agent)
await orchestrator.approve_gate(task_id: str) -> None

# Cancel via gate (stop or replan agent)
await orchestrator.reject_gate(task_id: str) -> None
```

The connector does not own gate state. If the orchestrator does not expose these methods, `/approve` and `/reject` respond with an ephemeral error.

### 4.2 asyncio.gather Pattern

Both the Discord bot and the orchestrator run as peers on the same event loop started by `asyncio.run(main())`:

```python
async def main():
    orchestrator = build_orchestrator()          # returns a fully-constructed orchestrator
    connector = DiscordConnector(
        token=os.environ["DISCORD_BOT_TOKEN"],
        orchestrator=orchestrator,
        webhook_url=os.environ.get("DISCORD_WEBHOOK_URL"),
    )
    await asyncio.gather(
        connector.start(),       # discord.py WebSocket + REST loop; runs until cancelled
        orchestrator.run(),      # LangGraph scheduler; runs until cancelled
    )

asyncio.run(main())
```

If either coroutine raises an unhandled exception, `asyncio.gather` propagates it and the process exits. Add a top-level `try/except` with logging in `main()` for graceful shutdown messages.

### 4.3 Slash Command Interaction Lifecycle

```
User invokes /task "build me a CLI tool"
       │
       ▼  [Discord gateway WebSocket → discord.py dispatch]
       │
task_command(interaction, description) called
       │
       ▼  [MUST complete within 3 seconds of invocation — HARD DEADLINE]
await interaction.response.defer(thinking=True)
       │  ACK sent to Discord immediately; shows "Bot is thinking…"
       │
       ▼
asyncio.create_task(_task_worker(interaction, record))
       │  Returns immediately; handler function exits
       │
       ▼  [background coroutine — no deadline pressure]
channel.create_thread(name="task-<id[:8]>")
       │
interaction.followup.send("Task started… ●", wait=True)
       │  Captures returned Message object for later edits
       │
orchestrator.stream(description)  ──► async generator yields tokens
       │
_streaming_worker accumulates tokens
       │  edits message at most 1x/second
       │  splits at 1900 chars into new messages
       │  checks 14.5-minute expiry on every token
       │
on stream end:
    _safe_edit(msg, buf, final=True)    — final flush, no streaming indicator
    _notify_completion(record)          — webhook POST
```

**The handler exits after `create_task`. It does not await the worker.** The worker is a fire-and-forget background coroutine. Errors surface via `worker.add_done_callback`.

### 4.4 Discord Rate Limits

Always parse `X-RateLimit-*` headers and `retry_after` from 429 bodies — do not hard-code values. These are the current known limits:

| Limit | Value | Scope | Notes |
|-------|-------|-------|-------|
| Global rate limit | ~50 requests/second | Per bot token | Across all routes |
| Message send | ~5 messages / 5 seconds | Per channel | Includes webhook messages |
| Message edit | ~5 edits / 5 seconds | Per message | Shared with sends on same channel |
| Webhook POST | ~5 messages / 5 seconds | Per webhook | Shared with channel limit |
| **Interaction ACK deadline** | **3 seconds** | Per interaction | **Hard deadline — miss = token invalidated** |
| **Followup window** | **15 minutes** | Per interaction token | All followup/edit calls fail after expiry |
| Slash command global sync | ~200 syncs / day | Per application | Use guild sync for development |
| Thread creation | ~5 / 5 seconds | Per channel | Shared with message sends |
| Message content | 2000 characters | Per message | Bots always 2000; Nitro does not apply |
| Embed description | 4096 characters | Per embed | Higher than message limit |

---

## 5. Implementation Steps

Work through these in order. Each step is independently testable.

### Step 1: DiscordConnector class skeleton with setup_hook()

**File:** `services/orchestrator/discord_connector.py`

Create the module with:

- Module-level docstring with the CRITICAL CONSTRAINT warning (copy verbatim from Key Code Patterns below)
- Constants: `EDIT_INTERVAL_SECONDS`, `MESSAGE_CHAR_LIMIT`, `STREAMING_INDICATOR`, `FOLLOWUP_EXPIRY_SECONDS`
- `TaskRecord` dataclass: `task_id`, `description`, `status`, `thread_id`, `started_at`, `asyncio_task`
- `DiscordConnector.__init__`: accepts `token`, `orchestrator`, `webhook_url`, `edit_interval`; builds `discord.ext.commands.Bot` with correct intents; initializes `_tasks: dict[str, TaskRecord]` and `_tasks_lock: asyncio.Lock()`
- `DiscordConnector._setup_hook`: syncs command tree; dev guild fast-sync if `DISCORD_DEV_GUILD_ID` is set
- `DiscordConnector.start`: calls `await self._bot.start(self._token)`

Verify: `python -c "from discord_connector import DiscordConnector; print('import ok')"` succeeds.

### Step 2: /task command handler — defer FIRST, then queue task

**File:** `services/orchestrator/discord_connector.py`, inside `_register_commands()`

Implement the `@tree.command(name="task")` handler. The handler body must be exactly:

1. `await interaction.response.defer(thinking=True)` — first line, no exceptions
2. Generate `task_id = str(uuid.uuid4())`
3. Build `TaskRecord` and store in `self._tasks` under the lock
4. `asyncio.create_task(self._task_worker(interaction, record))` — do NOT await
5. Attach `add_done_callback` for error logging
6. Return (handler exits; no further awaits)

Verify: Invoke `/task "hello"` in Discord. The bot shows "Bot is thinking…" within 3 seconds. Check logs for no errors.

### Step 3: _task_worker() and _streaming_worker() coroutines

**File:** `services/orchestrator/discord_connector.py`

`_task_worker` (background):

1. Check `interaction.guild is None` — if True, skip thread creation (DM fallback)
2. `channel.create_thread(name=f"task-{task_id[:8]}", type=public_thread, auto_archive_duration=60)`
3. `interaction.followup.send(content="...", wait=True)` — captures `initial_msg`
4. Calls `await self._streaming_worker(interaction, record, sink, initial_msg)`
5. `finally` block: set `record.status = "completed"` if still `"running"`; call `_notify_completion(record)`

`_streaming_worker` (token loop):

1. Initialize `buf = ""`, `current_msg = initial_msg`, `last_edit_at = time.monotonic()`, `started_at = time.monotonic()`
2. `async for token in self._generate(record.description):`
   - Check expiry guard (see Step 5)
   - `buf += token`
   - Check `len(buf) >= MESSAGE_CHAR_LIMIT` → call `_split_message` (see Step 4)
   - Check `time.monotonic() - last_edit_at >= self._edit_interval` → call `_safe_edit(current_msg, buf, final=False)`; update `last_edit_at`
3. After loop: `await self._safe_edit(current_msg, buf, final=True)` — final flush

`_generate` bridge:

```python
async def _generate(self, prompt: str) -> AsyncGenerator[str, None]:
    if hasattr(self._orchestrator, "stream"):
        async for token in self._orchestrator.stream(prompt):
            yield token
    else:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, self._orchestrator.run, prompt)
        yield result
```

Verify: Invoke `/task "write a haiku"`. The initial message appears within ~1 second and updates every second with new tokens until complete.

### Step 4: Auto-split at 1900 chars (split at last newline)

**File:** `services/orchestrator/discord_connector.py`

Implement `_split_message(sink, current_msg, buf) -> tuple[str, discord.Message]`:

1. `split_at = buf.rfind("\n", 0, MESSAGE_CHAR_LIMIT)` — find last newline within limit
2. If `split_at == -1`: `split_at = MESSAGE_CHAR_LIMIT` — hard cut fallback
3. `chunk = buf[:split_at]`; `remainder = buf[split_at:].lstrip("\n")`
4. `await self._safe_edit(current_msg, chunk, final=True)` — freeze old message
5. `new_msg = await sink.send(content=remainder + STREAMING_INDICATOR)` — start next message
6. Return `(remainder, new_msg)`

Called from `_streaming_worker` whenever `len(buf) >= MESSAGE_CHAR_LIMIT`.

Implement `_safe_edit(msg, content, final) -> None`:

1. `display = content if final else content + STREAMING_INDICATOR`
2. Retry loop up to 5 times:
   - `await msg.edit(content=display)` → return on success
   - On `discord.HTTPException` with `status == 429`: sleep `exc.retry_after` seconds, retry
   - On `status in (401, 404, 403)`: log and return silently (expired/deleted)
   - On any other status: log error and return

Verify: Run a task that generates more than 1,900 characters of output. The first message freezes and a second message appears continuing the stream.

### Step 5: 15-minute followup expiry guard

**File:** `services/orchestrator/discord_connector.py`

The guard lives inside the token loop in `_streaming_worker`. On every iteration, before processing the token:

```python
if time.monotonic() - started_at > FOLLOWUP_EXPIRY_SECONDS:
    buf += "\n\n[Output truncated: agent ran past the 15-minute Discord window.]"
    await self._safe_edit(current_msg, buf, final=True)
    record.status = "truncated"
    return
```

`FOLLOWUP_EXPIRY_SECONDS = 870.0` — 14.5 minutes, leaving a 30-second buffer before Discord's 15-minute hard cutoff.

The `_safe_edit` 401/404 handler provides a second layer: if the window has already closed when the edit fires, the exception is caught and swallowed silently.

Verify: Manually set `FOLLOWUP_EXPIRY_SECONDS = 5` in a dev environment. Run a long task. After 5 seconds the message should show the truncation notice and the worker should stop.

### Step 6: /status command

**File:** `services/orchestrator/discord_connector.py`, inside `_register_commands()`

```python
@tree.command(name="status", description="Check task status")
@app_commands.describe(task_id="Task ID prefix (omit to list all active tasks)")
async def status_command(interaction, task_id: Optional[str] = None):
    await interaction.response.defer(ephemeral=True, thinking=True)  # FIRST
    ...
```

Logic:

- If `task_id` provided: look up in `self._tasks` (prefix match on `task_id[:8]`); respond with status embed
- If omitted: build embed listing all tasks with ID, description snippet, status, elapsed time
- Response is always `ephemeral=True`

### Step 7: /cancel command

**File:** `services/orchestrator/discord_connector.py`, inside `_register_commands()`

```python
@tree.command(name="cancel", description="Cancel a running task")
@app_commands.describe(task_id="Task ID prefix (omit to cancel most recent active task)")
async def cancel_command(interaction, task_id: Optional[str] = None):
    await interaction.response.defer(ephemeral=True, thinking=True)  # FIRST
    ...
```

Logic:

- Resolve `task_id` → `TaskRecord` (most recent running if omitted)
- If not found or already completed: respond with informational ephemeral message
- Call `record.asyncio_task.cancel()` — raises `CancelledError` inside `_streaming_worker`
- `_streaming_worker`'s `finally` block (in `_task_worker`) sets `record.status = "cancelled"` and fires the webhook
- Respond with confirmation embed

Note: `asyncio.Task.cancel()` does not immediately cancel; it schedules `CancelledError` to be raised at the next `await` inside the task. The streaming loop will exit cleanly at the next `await` point (the `async for` iteration or a `_safe_edit` call).

### Step 8: /approve and /reject commands (HITL gates)

**File:** `services/orchestrator/discord_connector.py`, inside `_register_commands()`

```python
@tree.command(name="approve", description="Approve a pending human-in-the-loop gate")
@app_commands.describe(task_id="Task ID (optional)")
async def approve_command(interaction, task_id: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)  # FIRST
    ...

@tree.command(name="reject", description="Reject a pending human-in-the-loop gate")
@app_commands.describe(task_id="Task ID (optional)")
async def reject_command(interaction, task_id: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)  # FIRST
    ...
```

Logic (both commands):

1. Resolve `task_id` (most recent task if omitted)
2. Check `hasattr(self._orchestrator, "approve_gate")` — if not present, respond with ephemeral error
3. Check `self._orchestrator.pending_gate(task_id)` — if no pending gate, respond with "No pending gate for this task"
4. Call `await self._orchestrator.approve_gate(task_id)` or `await self._orchestrator.reject_gate(task_id)`
5. Respond with ephemeral confirmation

**SOTA note:** Consider replacing these slash commands with `discord.ui.View` buttons attached to the streaming message when the orchestrator pauses at a gate. This eliminates the need to remember command syntax. See `spec_integrations.md` §11.1 for the `HitlGateView` pattern.

### Step 9: Webhook notification on task completion

**File:** `services/orchestrator/discord_connector.py`

Implement `_notify_completion(record: TaskRecord) -> None`:

1. If `self._webhook_url` is None: return immediately (silently disabled)
2. Build `discord.Embed` with status emoji, title `"Task <id[:8]> — <STATUS>"`, description (first 200 chars of task description), color by status, thread link field if `record.thread_id` is set
3. `webhook = discord.Webhook.from_url(self._webhook_url, session=<aiohttp session>)`
4. `await webhook.send(embed=embed, username="Labmate")`
5. Wrap the entire thing in `try/except discord.HTTPException` — log failures, never propagate

For the aiohttp session: reuse the bot's internal session via `self._bot.http._HTTPClient__session`. This is a private attribute — if it breaks on a discord.py update, fall back to `aiohttp.ClientSession()` created during `_setup_hook`.

### Step 10: Wire into main.py with asyncio.gather

**File:** `services/orchestrator/main.py`

```python
import asyncio
import logging
import os

from dotenv import load_dotenv
from discord_connector import DiscordConnector
from orchestrator import build_orchestrator   # adjust import to actual orchestrator factory

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
log = logging.getLogger(__name__)

async def main() -> None:
    orchestrator = build_orchestrator()
    connector = DiscordConnector(
        token=os.environ["DISCORD_BOT_TOKEN"],
        orchestrator=orchestrator,
        webhook_url=os.environ.get("DISCORD_WEBHOOK_URL"),
        edit_interval=float(os.getenv("DISCORD_EDIT_INTERVAL", "1.0")),
    )
    try:
        await asyncio.gather(
            connector.start(),
            orchestrator.run(),
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Shutdown requested — exiting.")

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 6. Key Code Patterns

### THE CRITICAL CONSTRAINT: 3-second defer deadline

> **This is the single hardest constraint in the entire connector.** Discord invalidates the interaction token if `defer()` is not called within 3 seconds of the user invoking the slash command. There is no recovery path — the interaction is dead and no followup message can be sent.

Every slash command handler must follow this exact shape, with `defer()` as the first `await` and no exceptions:

```python
# ╔══════════════════════════════════════════════════════════════════════╗
# ║  WARNING: interaction.response.defer() MUST be the FIRST await.    ║
# ║  NO database lookups, NO orchestrator calls, NO thread creation,   ║
# ║  NO channel fetches before this line.                              ║
# ║  Discord gives you exactly 3 seconds. Miss it → interaction dead.  ║
# ╚══════════════════════════════════════════════════════════════════════╝
async def task_command(
    interaction: discord.Interaction,
    description: str,
) -> None:
    await interaction.response.defer(thinking=True)  # ← FIRST. ALWAYS. NO EXCEPTIONS.

    # All other work happens here, AFTER the ACK is sent.
    task_id = str(uuid.uuid4())
    record = TaskRecord(task_id=task_id, description=description)
    async with self._tasks_lock:
        self._tasks[task_id] = record
    worker = asyncio.create_task(
        self._task_worker(interaction, record),
        name=f"task-worker-{task_id[:8]}",
    )
    record.asyncio_task = worker
    worker.add_done_callback(
        lambda t: log.error("Task worker failed: %s", t.exception())
        if not t.cancelled() and t.exception()
        else None
    )
    # Handler exits here. Worker runs in background.
```

### _streaming_worker() with token accumulator and 1-second throttle

```python
async def _streaming_worker(
    self,
    interaction: discord.Interaction,
    record: TaskRecord,
    sink: discord.abc.Messageable,
    initial_msg: discord.Message,
) -> None:
    buf = ""
    current_msg = initial_msg
    last_edit_at = time.monotonic()
    started_at = time.monotonic()

    async for token in self._generate(record.description):
        # 15-minute expiry guard — checked on every token
        if time.monotonic() - started_at > FOLLOWUP_EXPIRY_SECONDS:
            log.warning("Task %s hit expiry guard — stopping stream", record.task_id)
            buf += "\n\n[Output truncated: agent ran past the 15-minute Discord window.]"
            await self._safe_edit(current_msg, buf, final=True)
            record.status = "truncated"
            return

        buf += token

        # Split at 1,900 chars
        if len(buf) >= MESSAGE_CHAR_LIMIT:
            buf, current_msg = await self._split_message(sink, current_msg, buf)

        # Throttled edit — at most 1x per EDIT_INTERVAL_SECONDS
        now = time.monotonic()
        if now - last_edit_at >= self._edit_interval:
            await self._safe_edit(current_msg, buf, final=False)
            last_edit_at = now

    # Final flush — remove streaming indicator
    if buf:
        await self._safe_edit(current_msg, buf, final=True)
```

### _split_message() splitting at last newline before 1900 chars

```python
async def _split_message(
    self,
    sink: discord.abc.Messageable,
    current_msg: discord.Message,
    buf: str,
) -> tuple[str, discord.Message]:
    # Find last newline within the limit for clean split; hard-cut if none
    split_at = buf.rfind("\n", 0, MESSAGE_CHAR_LIMIT)
    if split_at == -1:
        split_at = MESSAGE_CHAR_LIMIT

    chunk = buf[:split_at]
    remainder = buf[split_at:].lstrip("\n")

    # Freeze the current message without streaming indicator
    await self._safe_edit(current_msg, chunk, final=True)

    # Begin the next message with continuation text
    new_msg = await sink.send(
        content=(remainder + STREAMING_INDICATOR) if remainder else STREAMING_INDICATOR
    )
    return remainder, new_msg
```

### FOLLOWUP_EXPIRY_SECONDS = 870 guard

```python
# Constants — top of discord_connector.py
EDIT_INTERVAL_SECONDS: float = 1.0       # max 1 edit/second per message
MESSAGE_CHAR_LIMIT: int = 1_900          # split threshold; Discord hard cap is 2000
STREAMING_INDICATOR: str = " ●"     # " ●" — appended while agent is running
FOLLOWUP_EXPIRY_SECONDS: float = 870.0  # 14.5 minutes — 30s buffer before 15-min Discord cutoff

# Inside _streaming_worker, checked on every token:
if time.monotonic() - started_at > FOLLOWUP_EXPIRY_SECONDS:
    buf += "\n\n[Output truncated: agent ran past the 15-minute Discord window.]"
    await self._safe_edit(current_msg, buf, final=True)
    record.status = "truncated"
    return
```

### asyncio.gather in main.py

```python
async def main() -> None:
    orchestrator = build_orchestrator()
    connector = DiscordConnector(
        token=os.environ["DISCORD_BOT_TOKEN"],
        orchestrator=orchestrator,
        webhook_url=os.environ.get("DISCORD_WEBHOOK_URL"),
        edit_interval=float(os.getenv("DISCORD_EDIT_INTERVAL", "1.0")),
    )
    # Both coroutines share the same event loop created by asyncio.run().
    # connector.start() runs the discord.py WebSocket + REST machinery.
    # orchestrator.run() runs the LangGraph scheduler.
    # Neither blocks the other; they yield control via await at every I/O point.
    await asyncio.gather(
        connector.start(),
        orchestrator.run(),
    )

asyncio.run(main())
```

### 429 retry handler for discord edits (5 retries with Retry-After header)

```python
async def _safe_edit(
    self,
    msg: discord.Message,
    content: str,
    final: bool,
) -> None:
    display = content if final else (content + STREAMING_INDICATOR)

    for attempt in range(5):
        try:
            await msg.edit(content=display)
            return
        except discord.HTTPException as exc:
            if exc.status == 429:
                # Parse retry_after from the exception; default to 1.0s if absent
                retry_after = float(getattr(exc, "retry_after", None) or 1.0)
                log.warning(
                    "Rate limited editing message — waiting %.1fs (attempt %d/5)",
                    retry_after, attempt + 1,
                )
                await asyncio.sleep(retry_after)
                # Loop continues to retry
            elif exc.status in (401, 404, 403):
                # Interaction expired or message deleted — stop silently
                log.info(
                    "Edit returned %d — interaction likely expired or message deleted",
                    exc.status,
                )
                return
            else:
                log.error("Unexpected HTTP %d editing message: %s", exc.status, exc)
                return
    log.error("Gave up editing message after 5 rate-limited retries")
```

---

## 7. Integration Verification

Run these checks in order after implementation. Each must pass before moving to the next.

**7.1 Slash command registration**

- Start the bot with `DISCORD_DEV_GUILD_ID` set to your dev server
- In Discord, type `/` in any channel — confirm `/task`, `/status`, `/cancel`, `/approve`, `/reject` appear with correct descriptions
- If commands don't appear: check `_setup_hook` ran, check bot has `applications.commands` OAuth scope, check guild ID is correct integer

**7.2 3-second defer verification**

- Invoke `/task "hello world"`
- The "Bot is thinking…" placeholder must appear within 3 seconds of pressing Enter
- Check logs: no errors, `task_command` logged, worker task created
- Intentionally add `await asyncio.sleep(5)` before `defer()` in a test branch — confirm Discord shows "The application did not respond" — then revert

**7.3 Streaming edit verification**

- Invoke `/task "write a 500-word essay on asyncio"`
- A thread named `task-<id[:8]>` should appear in the channel
- The initial message shows the streaming indicator
- The message content updates approximately every second with new text
- On completion, the streaming indicator is removed from the final message

**7.4 1900-char auto-split verification**

- Invoke `/task "list every HTTP status code with a description"` (or a task that generates ~3000+ chars of output)
- The first message should freeze at or before 1,900 characters
- A second message should appear in the same thread immediately after, continuing the output
- The split should occur at a newline boundary — not mid-word
- Confirm no `discord.HTTPException: 400 — content: Must be 2000 or fewer in length` appears in logs

**7.5 15-minute expiry guard**

- Set `FOLLOWUP_EXPIRY_SECONDS = 10` in a dev run
- Invoke a long-running `/task`
- After ~10 seconds the message should show the truncation notice and stop updating
- Restore `FOLLOWUP_EXPIRY_SECONDS = 870.0` after verification

**7.6 Webhook notification**

- Set `DISCORD_WEBHOOK_URL` to a test webhook
- Complete a task successfully
- A formatted embed should appear in the webhook target channel with task ID, status COMPLETED, and thread link

**7.7 /cancel verification**

- Submit a long-running `/task`
- While it is streaming, invoke `/cancel`
- The streaming worker should stop within one iteration (at the next `await`)
- The webhook should fire with status `cancelled`
- The ephemeral cancel confirmation should be visible only to the invoking user

---

## 8. Done Criteria

- [ ] `discord_connector.py` exists at `services/orchestrator/discord_connector.py` and imports cleanly with no errors
- [ ] All five slash commands (`/task`, `/status`, `/cancel`, `/approve`, `/reject`) appear in Discord within the dev guild
- [ ] `/task` responds with "Bot is thinking…" within 3 seconds on every invocation — verified 10 consecutive times
- [ ] Streaming edits appear at most once per second; no HTTP 429 errors under normal load
- [ ] A 2,000+ character task response auto-splits into multiple messages at a newline boundary
- [ ] `FOLLOWUP_EXPIRY_SECONDS = 870.0` guard fires correctly (verified with short timeout in dev)
- [ ] `/status` returns correct task state; response is ephemeral
- [ ] `/cancel` stops a running task within one loop iteration; webhook fires with `cancelled` status
- [ ] `/approve` and `/reject` call the correct orchestrator gate methods or respond with ephemeral error if gates are not supported
- [ ] Webhook completion notification appears on task completion, failure, cancellation, and truncation
- [ ] Bot and orchestrator run together via `asyncio.gather` in `main.py`; no `asyncio` cross-loop warnings in logs
- [ ] DM fallback works: `/task` in a DM does not attempt thread creation and streams plain messages
- [ ] HTTP 429 on edit is retried up to 5 times using `retry_after` from the response; no unhandled exceptions
- [ ] `_safe_edit` swallows 401/404/403 silently (expired/deleted message) without crashing the worker

---

## 9. Common Mistakes

### Not calling defer() immediately — the most dangerous mistake

**Symptom:** Discord shows "The application did not respond." The interaction token is permanently invalidated. No followup message, no edit, no recovery.

**Cause:** Any `await` before `interaction.response.defer()` in the command handler — even a fast dict lookup that internally awaits, a `channel.fetch_message()`, or anything else — can push past the deadline under network jitter or event loop backpressure.

**Fix:** `defer()` is the first line of every command handler. Put a comment above it. Never move it. Treat any code placed above `defer()` as a bug.

```python
async def task_command(interaction, description):
    # WRONG — any work here before defer() is a potential 3-second timeout
    # task_id = await db.next_id()  ← DO NOT DO THIS

    await interaction.response.defer(thinking=True)  # CORRECT: first line, always
    # safe to do anything here
```

### Per-token edits

**Symptom:** Cascading HTTP 429 errors seconds after a task starts. Messages stop updating. Bot enters a retry loop it cannot escape.

**Cause:** Calling `message.edit()` on every yielded token. A 40 token/second LLM hits Discord's ~5 edits/5 second limit in under 1 second.

**Fix:** Accumulate tokens in `buf` and only call `_safe_edit` when `time.monotonic() - last_edit_at >= EDIT_INTERVAL_SECONDS`. The streaming indicator gives visual feedback that the agent is alive between edits.

### Missing the 1900-character message split

**Symptom:** `discord.HTTPException: 400 Bad Request — content: Must be 2000 or fewer in length.` The streaming message stops updating.

**Cause:** Allowing `buf` to grow past 2000 characters before calling `edit`. Unicode multi-byte characters and the streaming indicator add hidden length; the safe margin is 1,900 characters.

**Fix:** Check `len(buf) >= MESSAGE_CHAR_LIMIT` after every token append. Split before the buffer reaches 2000 characters. Never let a buffer that exceeds `MESSAGE_CHAR_LIMIT` reach `message.edit()`.

### Running the bot on a separate asyncio event loop (thread-based integration)

**Symptom:** `future.result()` deadlocks the calling loop. `asyncio.run_coroutine_threadsafe` results arrive unpredictably. Gateway heartbeat warnings: `"Shard ID None heartbeat blocked for more than 10 seconds"` followed by auto-reconnects.

**Cause:** Starting the discord.py client with `asyncio.new_event_loop()` in a background thread and bridging to the orchestrator's loop with `run_coroutine_threadsafe`. This creates two loops that cannot safely share state without locking every interaction.

**Fix:** Use a single shared event loop via `asyncio.gather(connector.start(), orchestrator.run())` launched by `asyncio.run(main())`. If the orchestrator exposes a synchronous API, use `await loop.run_in_executor(None, sync_fn, *args)` — never `future.result()` from inside the event loop.

### Not handling the 15-minute followup expiry

**Symptom:** `discord.HTTPException: 401 Unauthorized` or `404 Not Found` flooding logs approximately 15 minutes into a long task. Bot appears stuck. Users see no output after the expiry point.

**Cause:** Discord interaction tokens expire after 15 minutes. Any `followup.send()` or `message.edit()` after that raises an exception. Without a guard, the worker loops forever trying to edit a message it can no longer reach.

**Fix:** Track `started_at = time.monotonic()` when the worker begins. Check `time.monotonic() - started_at > FOLLOWUP_EXPIRY_SECONDS` on every token iteration. Append a truncation notice, call `_safe_edit` with `final=True`, set `record.status = "truncated"`, and return. Also wrap every `_safe_edit` call to catch and swallow 401/404 silently as a second layer of defense.

### Syncing the command tree in on_ready instead of setup_hook

**Symptom:** `tree.sync()` is called repeatedly as the bot reconnects, eventually hitting the ~200 global syncs/day limit. Commands appear to register but new commands don't propagate.

**Cause:** `on_ready` fires on every reconnect, not just startup. A bot that reconnects 10 times per day exhausts the sync budget in 20 days.

**Fix:** Call `await self._bot.tree.sync()` inside `setup_hook` (called exactly once before the first `on_ready`). For development, sync to a specific guild: `await self._bot.tree.sync(guild=discord.Object(id=DEV_GUILD_ID))` for instant propagation without consuming global quota.

### Blocking the event loop with synchronous orchestrator calls

**Symptom:** `"Shard ID None heartbeat blocked for more than 10 seconds"` in logs. Gateway disconnects and auto-reconnects even when the bot appears healthy.

**Cause:** Calling a synchronous blocking API (e.g., a blocking LLM client, file I/O, `time.sleep`) directly from a coroutine. This freezes the entire event loop, starving the discord.py heartbeat.

**Fix:** Every synchronous blocking call must be offloaded: `result = await loop.run_in_executor(None, sync_fn, *args)`. The `_generate` bridge method in the connector handles this automatically for synchronous orchestrators.
