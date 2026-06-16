# Integrations Spec (Discord Connector)

**Version:** 0.1.0
**Status:** Draft
**Scope:** Labmate — Discord as primary user-facing interface

---

## 1. Overview

The Discord connector is the sole external integration in Labmate's initial release. It serves as the primary user-facing interface through which users submit tasks, monitor progress, and manage the agent lifecycle. The connector exposes five slash commands (`/task`, `/status`, `/cancel`, `/approve`, `/reject`) and streams agent output back into dedicated per-task Discord threads via throttled message editing.

The connector is **not** a separate service. It runs as an async peer of the orchestrator on the same `asyncio` event loop, coordinated through `asyncio.gather`. This eliminates cross-loop bridging complexity and makes the entire system a single-process application that starts with `asyncio.run(main())`.

**Delivery mechanism:** edit-based streaming — the bot posts one placeholder message and edits it as the agent emits tokens, at a maximum rate of ~1 edit/second. When output exceeds 1,900 characters the bot splits into a new message and continues editing there. All task output is scoped to a dedicated thread, keeping the parent channel uncluttered.

**Notification path:** a Discord Webhook (separate from the gateway Bot) fires a formatted completion notification when the agent finishes, so users who watch for `@mention` pings receive a structured summary even if they navigated away from the thread.

---

## 2. Architecture

### 2.1 Shared Event Loop Integration (setup_hook + asyncio.gather)

Both the Discord bot and the orchestrator must share a single `asyncio` event loop. The recommended entry point:

```python
async def main():
    connector = DiscordConnector(token=TOKEN, orchestrator=orchestrator)
    await asyncio.gather(
        connector.start(),       # runs discord.py's internal WebSocket + REST loop
        orchestrator.run(),      # runs agent scheduling, tool calls, etc.
    )

asyncio.run(main())
```

The bot uses `setup_hook` (called once before the first `on_ready`) to register the command tree, start background housekeeping tasks, and acquire any resources that must outlive reconnects. `on_ready` is explicitly **not** used for one-time setup because it fires on every reconnect.

**Thread executor note:** If the orchestrator exposes only synchronous APIs (no `async`/`await`), wrap each invocation with `await loop.run_in_executor(ThreadPoolExecutor(max_workers=4), sync_fn, *args)`. Never call blocking code directly from a coroutine — the discord.py heartbeat will stall and the gateway will disconnect after ~10 seconds.

### 2.2 Slash Command → Defer → Worker → Followup Flow

Discord's interaction model imposes two hard deadlines:

| Deadline | Duration | What happens if missed |
|----------|----------|------------------------|
| **ACK (defer)** | **3 seconds** from command invocation | Discord shows "The application did not respond" and the interaction token is invalidated |
| **Followup** | 15 minutes from the original interaction | All followup/edit calls fail with HTTP 401 or 404 |

The full flow for `/task`:

```
User invokes /task
       │
       ▼  (must complete in < 3 s)
interaction.response.defer(thinking=True)   ← ACK the interaction NOW
       │
       ▼
asyncio.create_task(_task_worker(interaction, description))
       │  (returns immediately; handler exits)
       │
       ▼  (background coroutine)
channel.create_thread(name=task_id)
orchestrator.stream(description)  ─── async generator yields tokens
       │
       ▼
_streaming_worker accumulates tokens, edits message ~1x/sec
       │
       ▼
on completion: webhook.send(embed=completion_embed)
```

The `defer()` call must be the **first `await` in the command handler**. No database lookups, no thread creation, no orchestrator calls may precede it.

### 2.3 Edit-Based Streaming Pattern

Rather than sending a new message for each chunk of output, the bot:

1. Sends one initial placeholder message (`"⏳ Running…"`) via `interaction.followup.send(...)`, capturing the returned `Message` object.
2. Accumulates tokens into an in-memory buffer (`buf: str`).
3. On a 1-second timer (checked inside the token-yielding loop), calls `message.edit(content=buf + STREAMING_INDICATOR)`.
4. On final flush, calls `message.edit(content=buf)` without the indicator.

This means Discord sees at most ~1 edit/second per active task, well within the per-message edit rate limit (~5 edits/5 seconds per channel). The streaming indicator (e.g. `" ●"`) provides live visual feedback that the agent is still running.

**Why not per-token edits?** Discord's per-channel rate limit is approximately 5 messages (sends + edits) per 5 seconds. A 40 tokens/second LLM would saturate it within 1 second, triggering cascading HTTP 429s and eventual gateway pressure. Batching to 1 edit/second reduces API calls by ~40x.

### 2.4 Thread-Per-Task Session Isolation

Each `/task` invocation creates a public thread forked from the channel where the command was issued:

```python
thread = await channel.create_thread(
    name=f"task-{task_id[:8]}",
    type=discord.ChannelType.public_thread,
    auto_archive_duration=60,  # minutes
)
```

All streaming output and status updates for that task are posted exclusively into the thread. This provides:

- **Isolation:** concurrent tasks in the same channel do not intermix output.
- **History:** the full agent trace is preserved in the thread for post-hoc review.
- **Navigation:** Discord's thread panel lets users jump directly to any active task.

**DM fallback:** `channel.create_thread()` raises `discord.HTTPException` in DMs (threads are guild-only). Detect with `if interaction.guild is None:` and fall back to plain `interaction.followup.send()` messages without thread creation.

### 2.5 Bot vs Webhook Responsibilities

| Responsibility | Transport | Rationale |
|---|---|---|
| Slash command receipt | Bot (gateway) | Requires a persistent WebSocket connection and interaction token handling |
| Status / cancel / approve / reject | Bot (gateway) | Bidirectional: needs to read orchestrator state and respond |
| Streaming output edits | Bot (REST via `Message.edit`) | Tied to the original interaction followup message |
| Task completion notifications | Webhook (outbound REST POST) | Fire-and-forget; no reply needed; avoids gateway round-trips |
| File attachments | Bot (REST via `channel.send(file=)`) | Requires auth token for upload |

The webhook URL is stored as an environment variable (`DISCORD_WEBHOOK_URL`) and must be treated as a secret — it is unauthenticated and anyone with it can post to the channel.

### 2.6 ASCII Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                        asyncio event loop                   │
│                                                             │
│  ┌──────────────────┐        ┌───────────────────────────┐  │
│  │  discord.py Bot  │        │      Orchestrator         │  │
│  │  (gateway WS)    │        │   (agent + tool runner)   │  │
│  │                  │        │                           │  │
│  │  on /task ──────────────► create_task(_task_worker)   │  │
│  │  .defer() ◄──────────────  (immediate ACK)            │  │
│  │                  │        │                           │  │
│  │  _streaming_worker ◄──── orchestrator.stream()        │  │
│  │  (edit ~1/sec)   │        │   async generator         │  │
│  │                  │        │                           │  │
│  │  on_complete ───►│        │                           │  │
│  │    Webhook POST  │        │                           │  │
│  └──────────────────┘        └───────────────────────────┘  │
│            │                                                 │
└────────────┼─────────────────────────────────────────────────┘
             │
      Discord API (REST + Gateway)
             │
    ┌────────┴──────────┐
    │  Discord Client   │
    │  (user's app)     │
    │                   │
    │  #general         │
    │  └─ thread: task-abc123
    │       messages: streaming output
    └───────────────────┘
```

---

## 3. Key Design Decisions

### Why same event loop (not a separate thread)?

Running the bot and orchestrator on one `asyncio` event loop makes every interaction a native `await`: no `loop.call_soon_threadsafe`, no `asyncio.run_coroutine_threadsafe`, no `future.result()` blocking. The multi-loop alternative introduces two failure modes: (1) calling `future.result()` from inside the event loop deadlocks it immediately; (2) `run_coroutine_threadsafe` returns a `concurrent.futures.Future` whose `.result()` must never be called from the loop — easy to do accidentally in deeply nested code. A single-loop design makes these bugs impossible.

### Why edit-based streaming (not new messages per chunk)?

New messages count against the same per-channel rate limit (~5 messages/5 seconds). Sending a new message per second already approaches the limit; sending one per token instantly saturates it and triggers HTTP 429 cascades. Editing a single message is also better UX: the user reads a single coherent output that grows in place rather than a waterfall of fragmented messages. The 1-second edit interval is taken directly from the llmcord reference implementation and is the empirically safe minimum.

### Why thread-per-task (not per-channel)?

A channel shared across multiple concurrent tasks would mix their streaming outputs in timestamp order, making it impossible to read any single task's output. Threads provide first-class Discord UI isolation: each task gets its own scrollable history, its own notification subscription, and its own archive. The cost is one `create_thread` API call per task, which is negligible.

### Why bot + webhook split (not bot-only)?

Completion notifications do not need the interaction token (which expires in 15 minutes) or a live gateway connection. A webhook POST is a stateless HTTP call that can be retried independently and does not hold an event loop slot. Keeping notifications on the webhook path also means the notification still fires even if the bot's gateway connection is briefly interrupted.

---

## 4. Implementation

### 4.1 DiscordConnector Class

```python
"""
labmate/connectors/discord_connector.py

Primary user-facing interface for Labmate.

CRITICAL CONSTRAINT: interaction.response.defer() MUST be the first await
in every slash command handler. No exceptions. Any await before defer() risks
missing the 3-second Discord ACK deadline and invalidating the interaction token.

Architecture: single asyncio event loop shared with the orchestrator.
Start with: await asyncio.gather(connector.start(), orchestrator.run())
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Optional

import discord
from discord import app_commands

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EDIT_INTERVAL_SECONDS: float = 1.0      # max 1 edit/sec per message
MESSAGE_CHAR_LIMIT: int = 1_900         # split threshold (Discord hard cap: 2000)
STREAMING_INDICATOR: str = " ●"         # appended while agent is still running
TYPING_REFRESH_SECONDS: float = 8.0     # refresh interval for typing indicator
FOLLOWUP_EXPIRY_SECONDS: float = 870.0  # 14.5 min — leave 30s buffer before 15-min window


# ---------------------------------------------------------------------------
# Task state
# ---------------------------------------------------------------------------

@dataclass
class TaskRecord:
    task_id: str
    description: str
    status: str = "running"              # running | completed | cancelled | failed
    thread_id: Optional[int] = None
    started_at: float = field(default_factory=time.monotonic)
    asyncio_task: Optional[asyncio.Task] = None
```

### 4.2 Bot Initialization (setup_hook wiring)

```python
class DiscordConnector:
    """
    Wraps a discord.py Bot on the shared asyncio event loop.

    Parameters
    ----------
    token       : Discord bot token (from environment, never hard-coded)
    orchestrator: Agent orchestrator that exposes `.stream(prompt) -> AsyncGenerator`
                  or `.run(prompt) -> str` (sync fallback wrapped in run_in_executor)
    webhook_url : Discord webhook URL for completion notifications (optional)
    edit_interval: seconds between message edits during streaming (default 1.0)
    """

    def __init__(
        self,
        token: str,
        orchestrator: Any,
        webhook_url: Optional[str] = None,
        edit_interval: float = EDIT_INTERVAL_SECONDS,
    ) -> None:
        self._token = token
        self._orchestrator = orchestrator
        self._webhook_url = webhook_url
        self._edit_interval = edit_interval

        # Active tasks: task_id -> TaskRecord
        self._tasks: dict[str, TaskRecord] = {}
        self._tasks_lock = asyncio.Lock()

        # Bot setup
        intents = discord.Intents.default()
        intents.message_content = True        # required for @mention fallback
        self._bot = discord.ext.commands.Bot(
            command_prefix="!",               # unused but required by commands.Bot
            intents=intents,
        )

        # Wire setup_hook and command tree
        self._bot.setup_hook = self._setup_hook
        self._register_commands()

    async def _setup_hook(self) -> None:
        """
        Called ONCE before the first on_ready. Use for all one-time initialization.
        Do NOT use on_ready for one-time setup — it fires on every reconnect.
        """
        # Sync slash commands to all guilds the bot is in.
        # During development, sync to a specific guild for instant propagation:
        #   await self._bot.tree.sync(guild=discord.Object(id=DEV_GUILD_ID))
        # For production, global sync (up to 1 hour propagation):
        await self._bot.tree.sync()
        log.info("Slash command tree synced.")

    async def start(self) -> None:
        """Entry point — run under asyncio.gather alongside the orchestrator."""
        await self._bot.start(self._token)
```

### 4.3 /task Slash Command Handler (defer immediately)

```python
    def _register_commands(self) -> None:
        tree = self._bot.tree

        # ------------------------------------------------------------------
        # /task <description>
        # CRITICAL: defer() MUST be the first await. No database calls,
        # no thread creation, no orchestrator calls before defer().
        # ------------------------------------------------------------------
        @tree.command(name="task", description="Submit a new task to the agent")
        @app_commands.describe(description="What should the agent do?")
        async def task_command(
            interaction: discord.Interaction,
            description: str,
        ) -> None:
            # ╔══════════════════════════════════════════════════════════╗
            # ║  FIRST AWAIT — MUST happen within 3 seconds of invoke   ║
            # ║  thinking=True shows a "Bot is thinking…" placeholder   ║
            # ╚══════════════════════════════════════════════════════════╝
            await interaction.response.defer(thinking=True)

            # Everything below runs after the ACK is acknowledged.
            task_id = str(uuid.uuid4())
            record = TaskRecord(task_id=task_id, description=description)

            async with self._tasks_lock:
                self._tasks[task_id] = record

            # Spawn the streaming worker as a background task.
            # Do NOT await it here — that would block the handler and
            # violate the single-task-per-handler contract.
            worker = asyncio.create_task(
                self._task_worker(interaction, record),
                name=f"task-worker-{task_id[:8]}",
            )
            record.asyncio_task = worker

            # Attach an error logger so unhandled exceptions surface.
            worker.add_done_callback(
                lambda t: log.error("Task worker failed: %s", t.exception())
                if not t.cancelled() and t.exception()
                else None
            )
```

### 4.4 Streaming Worker (_streaming_worker coroutine)

```python
    async def _task_worker(
        self,
        interaction: discord.Interaction,
        record: TaskRecord,
    ) -> None:
        """
        Background coroutine that:
        1. Creates a thread for the task.
        2. Streams tokens from the orchestrator.
        3. Edits the Discord message at most once per second.
        4. Splits messages at 1,900 chars.
        5. Guards against the 15-minute followup expiry.
        6. Fires a webhook notification on completion.
        """
        channel = interaction.channel
        task_id = record.task_id

        # Determine if we're in a guild (threads are guild-only).
        in_guild = interaction.guild is not None

        # Create the task thread (guild only).
        thread: Optional[discord.Thread] = None
        if in_guild:
            try:
                thread = await channel.create_thread(
                    name=f"task-{task_id[:8]}",
                    type=discord.ChannelType.public_thread,
                    auto_archive_duration=60,
                )
                record.thread_id = thread.id
                log.info("Created thread %s for task %s", thread.id, task_id)
            except discord.HTTPException as exc:
                log.warning("Could not create thread: %s — falling back to channel", exc)
                thread = None

        sink = thread if thread else channel

        # Send the initial streaming placeholder via the interaction followup.
        try:
            initial_msg = await interaction.followup.send(
                content=f"Task `{task_id[:8]}` started… {STREAMING_INDICATOR}",
                wait=True,
            )
        except discord.HTTPException as exc:
            log.error("Failed to send initial followup: %s", exc)
            record.status = "failed"
            return

        # Run the streaming worker.
        try:
            await self._streaming_worker(
                interaction=interaction,
                record=record,
                sink=sink,
                initial_msg=initial_msg,
            )
        except Exception as exc:
            log.exception("Unhandled error in streaming worker for task %s", task_id)
            record.status = "failed"
        finally:
            async with self._tasks_lock:
                if record.status == "running":
                    record.status = "completed"

            await self._notify_completion(record)
```

### 4.5 Token Batching Accumulator

The accumulator collects tokens from the async generator and only flushes to Discord when the edit interval has elapsed or the token stream ends. This decouples the LLM output rate (potentially 40+ tokens/sec) from the Discord edit rate (~1/sec).

```python
    async def _streaming_worker(
        self,
        interaction: discord.Interaction,
        record: TaskRecord,
        sink: discord.abc.Messageable,
        initial_msg: discord.Message,
    ) -> None:
        """
        Core streaming loop. Accumulates tokens and edits the message ~1x/sec.
        Handles 1,900-char splits and the 15-minute expiry guard.
        """
        buf = ""                         # current message buffer
        current_msg = initial_msg        # message being edited
        last_edit_at = time.monotonic()  # timestamp of last edit
        started_at = time.monotonic()    # for 15-min expiry guard

        async for token in self._generate(record.description):
            # ── 15-minute expiry guard ──────────────────────────────────────
            if time.monotonic() - started_at > FOLLOWUP_EXPIRY_SECONDS:
                log.warning(
                    "Task %s approaching 15-min followup expiry — stopping stream",
                    record.task_id,
                )
                buf += "\n\n⚠️ _Output truncated: agent ran past the 15-minute Discord window._"
                await self._safe_edit(current_msg, buf, final=True)
                record.status = "truncated"
                return

            buf += token

            # ── 1,900-char split ────────────────────────────────────────────
            if len(buf) >= MESSAGE_CHAR_LIMIT:
                buf, current_msg = await self._split_message(
                    sink, current_msg, buf
                )

            # ── Throttled edit ──────────────────────────────────────────────
            now = time.monotonic()
            if now - last_edit_at >= self._edit_interval:
                await self._safe_edit(current_msg, buf, final=False)
                last_edit_at = now

        # Final flush — remove streaming indicator.
        if buf:
            await self._safe_edit(current_msg, buf, final=True)
```

### 4.6 Auto-Split at 1900 chars

When the buffer reaches 1,900 characters, the current message is finalized (frozen without the streaming indicator) and a new message is started in the same thread. The split attempts to preserve readability by breaking at the last newline within the limit.

```python
    async def _split_message(
        self,
        sink: discord.abc.Messageable,
        current_msg: discord.Message,
        buf: str,
    ) -> tuple[str, discord.Message]:
        """
        Finalize the current message and start a new one.

        Splits at the last newline within MESSAGE_CHAR_LIMIT to avoid
        breaking mid-word. Falls back to a hard cut if no newline exists.

        Returns (remaining_buf, new_message).
        """
        # Find last newline within the limit to split cleanly.
        split_at = buf.rfind("\n", 0, MESSAGE_CHAR_LIMIT)
        if split_at == -1:
            split_at = MESSAGE_CHAR_LIMIT  # hard cut if no newline found

        chunk = buf[:split_at]
        remainder = buf[split_at:].lstrip("\n")

        # Freeze the current message (no streaming indicator).
        await self._safe_edit(current_msg, chunk, final=True)

        # Start the next message with a continuation header.
        new_msg = await sink.send(
            content=remainder + STREAMING_INDICATOR if remainder else STREAMING_INDICATOR
        )
        return remainder, new_msg

    async def _safe_edit(
        self,
        msg: discord.Message,
        content: str,
        final: bool,
    ) -> None:
        """
        Edit a message with retry on HTTP 429 (rate limit).
        Silently swallows errors if the message/interaction is no longer valid
        (e.g., deleted by user, or 15-minute window already closed).
        """
        display = content if final else (content + STREAMING_INDICATOR)

        for attempt in range(5):
            try:
                await msg.edit(content=display)
                return
            except discord.HTTPException as exc:
                if exc.status == 429:
                    retry_after = float(
                        getattr(exc, "retry_after", None) or 1.0
                    )
                    log.warning(
                        "Rate limited on edit — retrying in %.1fs (attempt %d/5)",
                        retry_after,
                        attempt + 1,
                    )
                    await asyncio.sleep(retry_after)
                elif exc.status in (401, 404, 403):
                    # Interaction expired or message deleted — stop silently.
                    log.info(
                        "Edit failed with %d (likely expired interaction or deleted message)",
                        exc.status,
                    )
                    return
                else:
                    log.error("Unexpected HTTP error on edit: %s", exc)
                    return
```

### 4.7 Webhook Notification on Completion

```python
    async def _notify_completion(self, record: TaskRecord) -> None:
        """
        Send a completion notification via the outbound webhook.
        Uses a webhook (not the gateway) so no interaction token is needed.
        """
        if not self._webhook_url:
            return

        status_emoji = {
            "completed": "✅",
            "failed": "❌",
            "cancelled": "🚫",
            "truncated": "⚠️",
        }.get(record.status, "ℹ️")

        embed = discord.Embed(
            title=f"{status_emoji} Task {record.task_id[:8]} — {record.status.upper()}",
            description=record.description[:200],
            color={
                "completed": discord.Color.green(),
                "failed": discord.Color.red(),
                "cancelled": discord.Color.orange(),
                "truncated": discord.Color.yellow(),
            }.get(record.status, discord.Color.blurple()),
        )
        if record.thread_id:
            embed.add_field(name="Thread", value=f"<#{record.thread_id}>")

        try:
            webhook = discord.Webhook.from_url(
                self._webhook_url,
                session=self._bot.http._HTTPClient__session,  # reuse existing aiohttp session
            )
            await webhook.send(embed=embed, username="Labmate")
        except discord.HTTPException as exc:
            log.error("Webhook notification failed: %s", exc)

    async def _generate(self, prompt: str) -> AsyncGenerator[str, None]:
        """
        Bridge between DiscordConnector and the orchestrator's token stream.

        If the orchestrator exposes an async generator (.stream()), yield from it.
        If the orchestrator is synchronous (.run()), offload to a thread executor
        and yield the complete result as a single chunk.

        Orchestrators MUST NOT be called with a bare await/call from the event loop
        if they are blocking — use run_in_executor for sync APIs.
        """
        if hasattr(self._orchestrator, "stream"):
            # Preferred: orchestrator.stream() is an async generator.
            async for token in self._orchestrator.stream(prompt):
                yield token
        else:
            # Fallback: orchestrator.run() is a blocking synchronous call.
            loop = asyncio.get_running_loop()
            result: str = await loop.run_in_executor(
                None, self._orchestrator.run, prompt
            )
            yield result
```

### 4.8 Client Disconnect Guard (15-min followup expiry)

The 15-minute expiry guard is embedded in `_streaming_worker` (Section 4.5). The check is:

```python
if time.monotonic() - started_at > FOLLOWUP_EXPIRY_SECONDS:
```

`FOLLOWUP_EXPIRY_SECONDS = 870.0` (14.5 minutes) leaves a 30-second buffer before Discord invalidates the token. When triggered:

1. A truncation notice is appended to the buffer.
2. `_safe_edit` is called with `final=True` to freeze the message.
3. The worker returns early, setting `record.status = "truncated"`.
4. The completion webhook fires with the truncated status.

Additionally, `_safe_edit` handles the case where the expiry is hit mid-edit (Discord returns HTTP 401/404) by catching the exception and returning silently rather than looping into further errors.

---

## 5. Slash Commands

### 5.1 /task \<description\> — submit a new task

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `description` | string | yes | Natural language description of the task |

**Behavior:**
- `defer(thinking=True)` immediately (first await, no exceptions).
- Creates a public thread named `task-<id[:8]>`.
- Streams agent output into the thread via edit-based streaming.
- Responds with task ID in the initial followup so users can reference it in `/status` and `/cancel`.
- Falls back to channel messages (no thread) in DMs.

**Failure modes:**
- Orchestrator unavailable: respond via followup with error embed.
- Thread creation fails: fall back to parent channel.
- 15-minute expiry hit: truncate with notice, fire webhook.

### 5.2 /status [task_id] — check task status

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | no | Task ID prefix (omit to list all active tasks) |

**Behavior:**
- `defer(ephemeral=True, thinking=True)` immediately.
- If `task_id` provided: return status of that task (running/completed/failed/cancelled).
- If omitted: return a paginated embed listing all active tasks with IDs, start times, and statuses.
- Response is ephemeral (visible only to the invoking user).

### 5.3 /cancel [task_id] — cancel a running task

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | no | Task ID prefix (omit to cancel the most recent active task) |

**Behavior:**
- `defer(ephemeral=True, thinking=True)` immediately.
- Looks up the `asyncio.Task` in `_tasks[task_id].asyncio_task`.
- Calls `.cancel()` on the asyncio task, which raises `CancelledError` inside the streaming worker.
- The streaming worker's `finally` block sets `record.status = "cancelled"` and fires the webhook.
- Responds with confirmation embed (ephemeral).

**Failure modes:**
- Task not found: respond with error (ephemeral).
- Task already completed: respond with informational message.

### 5.4 /approve — approve a human-in-the-loop gate

**Behavior:**
- `defer(ephemeral=True)` immediately.
- Looks up the pending HITL gate for the invoking user's most recent task (or `task_id` if provided as optional param).
- Sets the gate's `asyncio.Event` or `Future` to unblock the orchestrator.
- Responds with `"Gate approved — agent resuming."` (ephemeral).

**Required orchestrator contract:** The orchestrator must expose a `pending_gate(task_id)` method and an async `approve_gate(task_id)` method. The connector does not own gate state.

### 5.5 /reject — reject a human-in-the-loop gate

**Behavior:**
- `defer(ephemeral=True)` immediately.
- Same lookup as `/approve`.
- Calls `reject_gate(task_id)` on the orchestrator, which should cancel the task or trigger a replanning step.
- Responds with `"Gate rejected — agent stopping."` (ephemeral).

---

## 6. BDD Test Scenarios

```gherkin
Feature: Streaming LLM response via throttled message editing

  Scenario: Agent streams a long answer without hitting rate limits
    Given a user invokes /task with a prompt that produces 500 tokens
    When the agent begins generating tokens at 40 tokens/second
    Then the bot sends one initial placeholder message with a streaming indicator
    And the bot edits that message at most once per second as tokens accumulate
    And when output exceeds 1900 characters the bot finalizes the current message
      and starts a new one in the same thread, splitting at the last newline
    And when generation completes the bot removes the streaming indicator
      from the final message

  Scenario: Message split preserves readability
    Given the agent emits output that crosses the 1900-character threshold mid-paragraph
    When the split is triggered
    Then the split occurs at the last newline before the 1900-character mark
    And if no newline exists in the last 1900 characters a hard cut is made
    And the next message begins with the continuation text

Feature: Non-blocking slash command backed by the agent orchestrator

  Scenario: Long-running agent task respects the interaction ACK deadline
    Given a user invokes /task which triggers a 40-second agent run
    When the command handler is entered
    Then interaction.response.defer(thinking=True) is called before any other await
    And the call completes within 3 seconds of the user's invocation
    And the agent runs in a background asyncio.Task without blocking the event loop
    And the final result is delivered via the followup within the 15-minute token window

  Scenario: Synchronous orchestrator does not block the event loop
    Given the orchestrator exposes only a synchronous .run() API
    When a /task command is invoked
    Then the connector wraps the call with loop.run_in_executor
    And the discord.py heartbeat continues uninterrupted during execution
    And the gateway does not emit "heartbeat blocked" warnings

Feature: Rate-limit-safe delivery

  Scenario: Bot receives HTTP 429 while editing a message
    Given the bot is editing a streamed response
    When Discord returns HTTP 429 with retry_after=1.2
    Then the bot waits 1.2 seconds before retrying the edit
    And the bot retries up to 5 times before giving up
    And the bot does not crash or drop the in-progress message

Feature: Per-task session isolation via threads

  Scenario: Two concurrent /task commands do not cross-contaminate output
    Given two /task commands are submitted concurrently in the same guild channel
    When both tasks begin streaming
    Then each task creates its own dedicated public thread
    And each task's streaming output appears only in its own thread
    And neither task's tokens appear in the other task's thread

  Scenario: /task in a DM falls back to plain messages
    Given a user invokes /task in a Direct Message (interaction.guild is None)
    When the task worker starts
    Then the bot does not attempt to create a thread
    And the bot streams output as plain followup messages in the DM

Feature: 15-minute expiry guard

  Scenario: Agent run exceeds the 14.5-minute safety threshold
    Given a task has been streaming for 870 seconds
    When the next token arrives
    Then the streaming worker appends a truncation notice to the buffer
    And calls _safe_edit with final=True to freeze the message
    And sets record.status to "truncated"
    And fires the completion webhook with truncated status
    And does not attempt further edits or followup calls

  Scenario: Discord returns HTTP 404 mid-stream (interaction expired)
    Given the streaming worker is editing a message
    When Discord returns HTTP 404 (interaction/message no longer valid)
    Then _safe_edit catches the exception and returns silently
    And the streaming worker stops without raising an unhandled exception

Feature: Human-in-the-loop gate

  Scenario: User approves a pending gate
    Given a task is paused at a HITL gate awaiting approval
    When the user invokes /approve
    Then the connector calls orchestrator.approve_gate(task_id)
    And the agent resumes from the gate
    And the user receives an ephemeral confirmation message

  Scenario: User rejects a pending gate
    Given a task is paused at a HITL gate
    When the user invokes /reject
    Then the connector calls orchestrator.reject_gate(task_id)
    And the agent stops or triggers replanning
    And the user receives an ephemeral rejection confirmation

Feature: Webhook completion notification

  Scenario: Webhook fires on task completion
    Given a task completes successfully
    When the streaming worker's finally block runs
    Then a Discord embed is POSTed to the configured webhook URL
    And the embed includes the task ID, status (COMPLETED), and thread link
    And if DISCORD_WEBHOOK_URL is not configured the notification is silently skipped

  Scenario: Webhook POST fails with HTTP 429
    Given the webhook rate limit is hit
    When discord.Webhook.send raises HTTPException with status 429
    Then the error is logged
    And the failure does not propagate to the task worker
    And the task is still marked completed in _tasks
```

---

## 7. Common Pitfalls

### Pitfall 1: Not deferring within 3 seconds (hardest constraint)

**Symptom:** Discord shows "The application did not respond." Interaction token is invalidated — no followup, no edit, no nothing.

**Cause:** Any `await` before `interaction.response.defer()` in the command handler — even a fast database lookup, a Redis check, or a `channel.fetch_message()` — can push past the deadline under load or network jitter.

**Fix:** `defer()` must be the **first `await`**. Comment it prominently. Lint it. The pattern:
```python
async def task_command(interaction, description):
    await interaction.response.defer(thinking=True)  # FIRST. ALWAYS. NO EXCEPTIONS.
    # everything else below
```

### Pitfall 2: Per-token edits

**Symptom:** Cascading HTTP 429 errors within seconds of starting a stream. Messages stop updating. Bot enters a retry death spiral.

**Cause:** Calling `message.edit()` on every yielded token. A 40 token/sec LLM saturates the ~5 edits/5s per-channel rate limit in 0.6 seconds.

**Fix:** Accumulate tokens in `buf` and only call `message.edit()` when `time.monotonic() - last_edit_at >= EDIT_INTERVAL_SECONDS`.

### Pitfall 3: Missing the 2000-character (1900-char safe) split

**Symptom:** `discord.HTTPException: 400 Bad Request (error code: 50035): Invalid Form Body — content: Must be 2000 or fewer in length.`

**Cause:** Allowing `buf` to grow past 2000 characters before editing or sending.

**Fix:** Check `len(buf) >= MESSAGE_CHAR_LIMIT` (1900) after every token append and split before calling `edit`. The 100-character margin absorbs the streaming indicator and any edge-case Unicode multi-byte surprises.

### Pitfall 4: Running the bot on a separate asyncio event loop

**Symptom:** `future.result()` deadlocks the calling loop. `run_coroutine_threadsafe` calls return but results arrive unpredictably. Gateway heartbeat warnings appear even when no blocking call is obvious.

**Cause:** Starting the discord.py client in a background thread with `asyncio.new_event_loop()` and attempting to bridge to the orchestrator's loop.

**Fix:** Use a single shared event loop via `asyncio.gather(connector.start(), orchestrator.run())`. If the orchestrator is synchronous, use `loop.run_in_executor`, never `future.result()` from inside the loop.

### Pitfall 5: Unhandled 15-minute followup expiry

**Symptom:** `discord.HTTPException: 401 Unauthorized` or `404 Not Found` flooding logs ~15 minutes into a long task. Bot appears stuck.

**Cause:** The interaction token becomes invalid after 15 minutes. Any `followup.send()` or `message.edit()` after that raises an exception.

**Fix:** Track `started_at = time.monotonic()` at worker start. Check against `FOLLOWUP_EXPIRY_SECONDS = 870` (14.5 minutes) on each token iteration. Also wrap every `_safe_edit` call to silently swallow 401/404 responses.

### Pitfall 6: Webhook rate limiting during burst completions

**Symptom:** Completion notifications fail with HTTP 429 when many tasks finish near-simultaneously.

**Cause:** Discord webhooks share rate limits with the channel (~5 messages/5s). Burst completions can hit the limit.

**Fix:** Wrap `webhook.send()` in try/except, log failures, and — for critical notifications — implement exponential backoff with up to 3 retries. Do not let webhook failures propagate to the task worker.

### Pitfall 7: DM vs guild context mismatch

**Symptom:** `discord.HTTPException: 403 Forbidden` or `400 Bad Request` when `/task` is invoked in a DM. `channel.create_thread()` fails.

**Cause:** Discord threads are guild-only. `create_thread()` on a DM channel raises an exception.

**Fix:** Check `if interaction.guild is None:` before calling `create_thread()` and skip thread creation, falling back to plain followup messages in the DM.

### Pitfall 8: Syncing the command tree in on_ready

**Symptom:** `tree.sync()` is called dozens of times as the bot reconnects, hitting the global command sync rate limit. Commands appear to register but stop syncing after repeated reconnects.

**Cause:** `on_ready` fires on every reconnect, not just startup.

**Fix:** Sync in `setup_hook` (called once) or via an owner-only prefix command invoked manually when commands change. Never call `tree.sync()` in `on_ready`.

### Pitfall 9: Blocking the heartbeat with synchronous LLM calls

**Symptom:** `discord.gateway: Shard ID None heartbeat blocked for more than 10 seconds.` followed by gateway disconnects and auto-reconnects.

**Cause:** Calling a synchronous LLM or orchestrator API directly from a coroutine (e.g., `result = orchestrator.run_sync(prompt)` without `run_in_executor`).

**Fix:** Any synchronous blocking call must go through `await loop.run_in_executor(executor, sync_fn, *args)`. The entire `asyncio.gather` loop must remain free at all times.

---

## 8. Rate Limits Reference

All limits are per Discord's official documentation. They change without notice; always parse `X-RateLimit-*` response headers and `retry_after` from HTTP 429 bodies rather than hard-coding values.

| Limit | Value | Scope | Notes |
|-------|-------|-------|-------|
| Global rate limit | ~50 requests/second | Per bot token | Applies across all routes |
| Message send | ~5 messages / 5 seconds | Per channel | Includes bot and webhook messages |
| Message edit | ~5 edits / 5 seconds | Per message | Shared with sends on same channel |
| Webhook POST | ~5 messages / 5 seconds | Per webhook | Shared with channel limit |
| Interaction ACK deadline | **3 seconds** | Per interaction | Hard deadline; miss = token invalidated |
| Followup window | **15 minutes** | Per interaction token | All followup/edit calls fail after |
| Slash command global sync | ~200 syncs / day | Per application | Use guild sync for development |
| Thread creation | ~5 / 5 seconds | Per channel | Shared with message sends |
| File upload max | 25 MiB per request | Per request | Nitro/boost does NOT apply to bots |
| Embed limits | 4096 chars (description), 6000 chars (total) | Per embed | Higher than message limit |
| Message content | 2000 characters | Per message | 4000 for Nitro users (bots: always 2000) |

**Parsing 429 responses in discord.py:** discord.py handles most rate limits automatically. For manual webhook calls or custom HTTP sessions, parse:
```
X-RateLimit-Limit: 5
X-RateLimit-Remaining: 0
X-RateLimit-Reset-After: 1.234
Retry-After: 1.234  (body: {"retry_after": 1.234, "global": false})
```

---

## 9. Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `discord.py` | `>=2.4.0` | Gateway client, REST wrapper, app_commands (slash commands), Views/Modals |
| `aiohttp` | `>=3.9.0` | Async HTTP (bundled with discord.py; used for webhook calls) |
| `uvloop` | `>=0.19.0` | Drop-in faster event loop (UNIX only; optional but recommended in production) |
| `python-dotenv` | `>=1.0.0` | Environment variable loading for `DISCORD_BOT_TOKEN`, `DISCORD_WEBHOOK_URL` |

**Environment variables:**

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_BOT_TOKEN` | yes | Bot token from Discord Developer Portal |
| `DISCORD_WEBHOOK_URL` | no | Webhook URL for completion notifications; omit to disable |
| `DISCORD_DEV_GUILD_ID` | no | Guild ID for instant slash command sync during development |
| `DISCORD_EDIT_INTERVAL` | no | Edit throttle in seconds (default: `1.0`) |

**Required Discord bot permissions and intents:**

- Intents: `message_content` (for `@mention` fallback), `guilds`, `guild_messages`
- OAuth2 scopes: `bot`, `applications.commands`
- Bot permissions: `Send Messages`, `Create Public Threads`, `Send Messages in Threads`, `Read Message History`, `Use Slash Commands`

---

## 10. Reference Repos

| Repository | Language | Relevance |
|------------|----------|-----------|
| [jakobdylanc/llmcord](https://github.com/jakobdylanc/llmcord) | Python | Canonical ~200-line discord.py LLM bot; reply-chain context, throttled edit streaming (`EDIT_DELAY_SECONDS=1`), auto message splitting at 2000 chars, mutex-protected message cache, OpenAI-compatible API layer. Primary reference for the streaming pattern. |
| [OoriData/Discord-AI-Agent](https://github.com/OoriData/Discord-AI-Agent) | Python | discord.py + MCP integration, pgvector message history, SentenceTransformer embeddings. Reference for MCP-based tool exposure and persistent memory. |
| [braindead-dev/DARVIS](https://github.com/braindead-dev/DARVIS) | JavaScript (discord.js) | Agentic bot with dynamic Discord API code generation; message pipeline: Message → Filter → Build Context → LLM → Execute Actions → Respond. Reference for agentic action pipelines. |
| [Rapptz/discord.py](https://github.com/Rapptz/discord.py) | Python | Core library. Discussion #9498 covers command-sync guidance (sync in `setup_hook`, not `on_ready`). |
| [kkrypt0nn/Python-Discord-Bot-Template](https://github.com/kkrypt0nn/Python-Discord-Bot-Template) | Python | Production-grade discord.py template with cogs, sync handling, and error handling patterns. |

**Official Discord documentation:**
- [Rate Limits](https://discord.com/developers/docs/topics/rate-limits) — token buckets, global 50 req/s, 429 handling
- [Receiving and Responding to Interactions](https://discord.com/developers/docs/interactions/receiving-and-responding) — 3-second ACK, 15-minute token, defer/followup model
- [Channel Resource](https://discord.com/developers/docs/resources/channel) — threads, file uploads, 25 MiB cap

---

## 11. SOTA Improvements

The following improvements go beyond the baseline spec and represent the current state of the art for Discord-based AI agent interfaces. They are not required for v0.1 but are candidates for the roadmap.

### 11.1 Discord UI Components for HITL Approval

Replace the `/approve` and `/reject` slash commands with in-message `discord.ui.View` Buttons. The orchestrator pauses at a gate and the streaming message gets a `[✅ Approve] [❌ Reject]` button row appended. Users click directly in the thread without memorizing commands.

```python
class HitlGateView(discord.ui.View):
    def __init__(self, connector, task_id):
        super().__init__(timeout=300)  # 5-minute gate timeout
        self.connector = connector
        self.task_id = task_id

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        await self.connector._orchestrator.approve_gate(self.task_id)
        self.stop()

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        await self.connector._orchestrator.reject_gate(self.task_id)
        self.stop()
```

### 11.2 Forum Channels for Task Organization

Discord Forum Channels (guild-only, requires `FORUM` channel type) provide a structured alternative to threads: each task becomes a Post with tags (e.g., `running`, `completed`, `failed`). Tags update automatically as task status changes. This provides a native task-management UI without any custom frontend.

Requires: `channel.type == discord.ChannelType.forum`, `channel.create_thread()` with `applied_tags`.

### 11.3 Voice Output (TTS)

For completed short-form tasks, synthesize audio via a TTS service (e.g., OpenAI TTS, Google Cloud TTS) and post the result as an audio file attachment. Discord supports playback of `.mp3` and `.ogg` attachments inline.

Constraint: 25 MiB max file size (bots do not benefit from Nitro/boost increases).

### 11.4 Discord Activities (Iframe Embeds)

Discord Activities allow embedding a custom web UI directly in a Discord voice channel via an `iframe`. This could render a rich task dashboard (progress bars, structured output, file trees) rather than raw text. Requires the `EMBEDDED_APPLICATION` OAuth2 scope and a deployed HTTPS frontend.

This is the highest-effort improvement but provides the most sophisticated UX — effectively a custom Claude Code-style terminal UI rendered inside Discord.

### 11.5 Ephemeral Status Responses

All `/status` and `/cancel` responses should be `ephemeral=True` (visible only to the invoking user) to avoid cluttering shared channels with status noise. Streaming output goes to the thread; meta-commands stay private.

### 11.6 Reply-Chain Conversation Context (llmcord pattern)

Derive multi-turn conversation context from Discord's native reply graph rather than a custom session store. When a user replies to a bot message, traverse the reply chain up to a configurable depth and include prior messages as context. Cache fetched messages in a mutex-protected `dict[int, discord.Message]` bounded by LRU eviction to avoid unbounded memory growth.

### 11.7 Backend-Agnostic LLM Layer

Wrap the orchestrator's LLM calls with an OpenAI-compatible client (`litellm`) so the connector works with any backend (Ollama, LM Studio, vLLM, OpenRouter, Anthropic, OpenAI) without code changes. Expose a `/model <name>` slash command for runtime switching between backends.
