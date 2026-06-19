"""
services/connectors/discord_connector.py

Primary user-facing interface for Labmate via Discord.

CRITICAL CONSTRAINT: interaction.response.defer() MUST be the first await
in every slash command handler. No exceptions. Any await before defer() risks
missing the 3-second Discord ACK deadline and invalidating the interaction token.

Architecture: single asyncio event loop shared with the orchestrator.
Start with: await asyncio.gather(connector.start(), orchestrator.run())

Environment variables:
  DISCORD_BOT_TOKEN     required — bot token from Discord Developer Portal
  DISCORD_WEBHOOK_URL   optional — webhook URL for completion notifications
  DISCORD_DEV_GUILD_ID  optional — guild ID for instant slash command sync (dev)
  DISCORD_EDIT_INTERVAL optional — edit throttle in seconds (default 1.0)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Optional

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EDIT_INTERVAL_SECONDS: float = float(os.getenv("DISCORD_EDIT_INTERVAL", "1.0"))
MESSAGE_CHAR_LIMIT: int = 1_900       # Discord hard cap is 2000; 100-char margin
STREAMING_INDICATOR: str = " ●"
FOLLOWUP_EXPIRY_SECONDS: float = 870.0   # 14.5 min — 30s buffer before 15-min window


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _render_result(state: Any) -> str:
    """Convert the orchestrator final_state into displayable text for Discord."""
    if state is None:
        return "_(empty result)_"
    if isinstance(state, str):
        return state
    if isinstance(state, dict):
        # Check direct final_answer first (set by graph's check node)
        for key in ("final_answer", "result", "answer", "output"):
            val = state.get(key)
            if val and isinstance(val, str) and val.strip():
                return val
        # Fall back to root goal result in goal_tree
        goal_tree = state.get("goal_tree", {})
        root = goal_tree.get("root", {})
        root_result = root.get("result", "")
        if root_result:
            return root_result
        # Last resort: pretty JSON (at least truncate it)
        dumped = json.dumps(state, indent=2, default=str)
        return dumped[:3000] + ("\n…(truncated)" if len(dumped) > 3000 else "")
    return str(state)


# ---------------------------------------------------------------------------
# Task record
# ---------------------------------------------------------------------------

@dataclass
class TaskRecord:
    task_id: str
    description: str
    status: str = "running"          # running | completed | cancelled | failed | truncated
    thread_id: Optional[int] = None
    started_at: float = field(default_factory=time.monotonic)
    asyncio_task: Optional[asyncio.Task] = None


# ---------------------------------------------------------------------------
# Main connector class
# ---------------------------------------------------------------------------

class DiscordConnector:
    """
    Wraps a discord.py Bot on the shared asyncio event loop.

    Parameters
    ----------
    token         : Discord bot token (from environment, never hard-coded)
    orchestrator  : Agent orchestrator that exposes .stream(prompt) → AsyncGenerator
                    or .run(prompt) → str (sync fallback via run_in_executor)
    webhook_url   : Discord webhook URL for completion notifications (optional)
    edit_interval : seconds between message edits during streaming (default 1.0)
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

        self._tasks: dict[str, TaskRecord] = {}
        self._tasks_lock = asyncio.Lock()

        self._redis = None  # Will be initialized in start()

        intents = discord.Intents.default()
        intents.message_content = True
        self._bot = commands.Bot(command_prefix="!", intents=intents)

        # Wire setup_hook and slash commands before the event loop starts.
        self._bot.setup_hook = self._setup_hook
        self._register_commands()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def _setup_hook(self) -> None:
        """Called once before the first on_ready. Sync the command tree here, never in on_ready."""
        dev_guild_id = os.getenv("DISCORD_DEV_GUILD_ID")
        if dev_guild_id:
            guild = discord.Object(id=int(dev_guild_id))
            self._bot.tree.copy_global_to(guild=guild)
            await self._bot.tree.sync(guild=guild)
            log.info("Slash commands synced to dev guild %s (instant propagation)", dev_guild_id)
        else:
            await self._bot.tree.sync()
            log.info("Slash commands synced globally (up to 1 hour propagation)")

    async def start(self) -> None:
        """Entry point — run under asyncio.gather alongside the orchestrator."""
        import redis.asyncio as aioredis
        self._redis = aioredis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            decode_responses=False,
        )
        await self._bot.start(self._token)

    # ── Slash command registration ─────────────────────────────────────────

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
            # ║  FIRST AWAIT — within 3 seconds of invocation           ║
            # ╚══════════════════════════════════════════════════════════╝
            await interaction.response.defer(thinking=True)

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
                lambda t: log.error("task-worker-%s failed: %s", task_id[:8], t.exception())
                if not t.cancelled() and t.exception()
                else None
            )

        # ------------------------------------------------------------------
        # /status [task_id]
        # ------------------------------------------------------------------
        @tree.command(name="status", description="Check task status")
        @app_commands.describe(task_id="Task ID prefix (omit to list all active tasks)")
        async def status_command(
            interaction: discord.Interaction,
            task_id: Optional[str] = None,
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)

            async with self._tasks_lock:
                snapshot = dict(self._tasks)

            if task_id:
                matches = [r for tid, r in snapshot.items() if tid.startswith(task_id)]
                if not matches:
                    await interaction.followup.send(
                        f"No task found with prefix `{task_id}`.", ephemeral=True
                    )
                    return
                r = matches[0]
                embed = discord.Embed(
                    title=f"Task {r.task_id[:8]}",
                    description=r.description[:200],
                    color=_status_color(r.status),
                )
                embed.add_field(name="Status", value=r.status.upper())
                if r.thread_id:
                    embed.add_field(name="Thread", value=f"<#{r.thread_id}>")
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                active = [r for r in snapshot.values() if r.status == "running"]
                if not active:
                    await interaction.followup.send("No active tasks.", ephemeral=True)
                    return
                embed = discord.Embed(title=f"Active tasks ({len(active)})")
                for r in active[:25]:
                    elapsed = int(time.monotonic() - r.started_at)
                    embed.add_field(
                        name=f"`{r.task_id[:8]}`",
                        value=f"{r.description[:80]} — {elapsed}s",
                        inline=False,
                    )
                await interaction.followup.send(embed=embed, ephemeral=True)

        # ------------------------------------------------------------------
        # /cancel [task_id]
        # ------------------------------------------------------------------
        @tree.command(name="cancel", description="Cancel a running task")
        @app_commands.describe(task_id="Task ID prefix (omit to cancel the most recent task)")
        async def cancel_command(
            interaction: discord.Interaction,
            task_id: Optional[str] = None,
        ) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)

            async with self._tasks_lock:
                snapshot = dict(self._tasks)

            if task_id:
                matches = [r for tid, r in snapshot.items() if tid.startswith(task_id)]
            else:
                running = sorted(
                    [r for r in snapshot.values() if r.status == "running"],
                    key=lambda r: r.started_at,
                    reverse=True,
                )
                matches = running[:1]

            if not matches:
                await interaction.followup.send("No matching task found.", ephemeral=True)
                return

            r = matches[0]
            if r.status != "running":
                await interaction.followup.send(
                    f"Task `{r.task_id[:8]}` is already `{r.status}`.", ephemeral=True
                )
                return

            if r.asyncio_task and not r.asyncio_task.done():
                r.asyncio_task.cancel()
                r.status = "cancelled"
                await interaction.followup.send(
                    f"Task `{r.task_id[:8]}` cancelled.", ephemeral=True
                )
            else:
                await interaction.followup.send(
                    f"Task `{r.task_id[:8]}` has no active asyncio task.", ephemeral=True
                )

        # ------------------------------------------------------------------
        # /approve  — resume a HITL gate
        # ------------------------------------------------------------------
        @tree.command(name="approve", description="Approve a pending human-in-the-loop gate")
        @app_commands.describe(task_id="Task ID prefix (omit for the most recent pending gate)")
        async def approve_command(
            interaction: discord.Interaction,
            task_id: Optional[str] = None,
        ) -> None:
            await interaction.response.defer(ephemeral=True)
            tid = await self._resolve_gate_task_id(task_id)
            if tid is None:
                await interaction.followup.send("No pending gate found.", ephemeral=True)
                return
            await self._orchestrator.approve_gate(tid)
            await interaction.followup.send("Gate approved — agent resuming.", ephemeral=True)

        # ------------------------------------------------------------------
        # /reject  — abort a HITL gate
        # ------------------------------------------------------------------
        @tree.command(name="reject", description="Reject a pending human-in-the-loop gate")
        @app_commands.describe(task_id="Task ID prefix (omit for the most recent pending gate)")
        async def reject_command(
            interaction: discord.Interaction,
            task_id: Optional[str] = None,
        ) -> None:
            await interaction.response.defer(ephemeral=True)
            tid = await self._resolve_gate_task_id(task_id)
            if tid is None:
                await interaction.followup.send("No pending gate found.", ephemeral=True)
                return
            await self._orchestrator.reject_gate(tid)
            await interaction.followup.send("Gate rejected — agent stopping.", ephemeral=True)

    # ── Task worker ────────────────────────────────────────────────────────

    async def _task_worker(
        self,
        interaction: discord.Interaction,
        record: TaskRecord,
    ) -> None:
        channel = interaction.channel
        task_id = record.task_id
        in_guild = interaction.guild is not None

        thread: Optional[discord.Thread] = None
        if in_guild:
            try:
                thread = await channel.create_thread(
                    name=f"task-{task_id[:8]}",
                    type=discord.ChannelType.public_thread,
                    auto_archive_duration=60,
                )
                record.thread_id = thread.id
                log.info("created thread %s for task %s", thread.id, task_id)
            except discord.HTTPException as exc:
                log.warning("could not create thread: %s — falling back to channel", exc)
                thread = None

        sink = thread if thread else channel

        try:
            initial_msg = await interaction.followup.send(
                content=f"Task `{task_id[:8]}` started… {STREAMING_INDICATOR}",
                wait=True,
            )
        except discord.HTTPException as exc:
            log.error("failed to send initial followup: %s", exc)
            record.status = "failed"
            return

        try:
            await self._streaming_worker(interaction, record, sink, initial_msg)
        except asyncio.CancelledError:
            record.status = "cancelled"
            try:
                await initial_msg.edit(content=f"Task `{task_id[:8]}` cancelled.")
            except discord.HTTPException:
                pass
        except Exception:
            log.exception("unhandled error in streaming worker for task %s", task_id)
            record.status = "failed"
        finally:
            async with self._tasks_lock:
                if record.status == "running":
                    record.status = "completed"
            await self._notify_completion(record)

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

        async for token in self._generate(record.description, task_id=record.task_id):
            # 15-minute expiry guard
            if time.monotonic() - started_at > FOLLOWUP_EXPIRY_SECONDS:
                log.warning("task %s approaching 15-min followup expiry — stopping stream", record.task_id)
                buf += "\n\n⚠️ _Output truncated: agent ran past the 15-minute Discord window._"
                await self._safe_edit(current_msg, buf, final=True)
                record.status = "truncated"
                return

            buf += token

            # 1900-char split
            if len(buf) >= MESSAGE_CHAR_LIMIT:
                buf, current_msg = await self._split_message(sink, current_msg, buf)

            # Throttled edit (~1/sec)
            now = time.monotonic()
            if now - last_edit_at >= self._edit_interval:
                await self._safe_edit(current_msg, buf, final=False)
                last_edit_at = now

        # Final flush — remove streaming indicator
        if buf:
            await self._safe_edit(current_msg, buf, final=True)

    # ── Message helpers ────────────────────────────────────────────────────

    async def _split_message(
        self,
        sink: discord.abc.Messageable,
        current_msg: discord.Message,
        buf: str,
    ) -> tuple[str, discord.Message]:
        split_at = buf.rfind("\n", 0, MESSAGE_CHAR_LIMIT)
        if split_at == -1:
            split_at = MESSAGE_CHAR_LIMIT

        chunk = buf[:split_at]
        remainder = buf[split_at:].lstrip("\n")

        await self._safe_edit(current_msg, chunk, final=True)

        new_msg = await sink.send(
            content=(remainder + STREAMING_INDICATOR) if remainder else STREAMING_INDICATOR
        )
        return remainder, new_msg

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
                    retry_after = float(getattr(exc, "retry_after", None) or 1.0)
                    log.warning(
                        "rate limited on edit — retrying in %.1fs (attempt %d/5)",
                        retry_after, attempt + 1,
                    )
                    await asyncio.sleep(retry_after)
                elif exc.status in (401, 404, 403):
                    log.info("edit failed with %d (interaction expired or message deleted)", exc.status)
                    return
                else:
                    log.error("unexpected HTTP error on edit: %s", exc)
                    return

    async def _notify_completion(self, record: TaskRecord) -> None:
        if not self._webhook_url:
            return

        status_emoji = {
            "completed": "✅", "failed": "❌",
            "cancelled": "🚫", "truncated": "⚠️",
        }.get(record.status, "ℹ️")

        embed = discord.Embed(
            title=f"{status_emoji} Task {record.task_id[:8]} — {record.status.upper()}",
            description=record.description[:200],
            color=_status_color(record.status),
        )
        if record.thread_id:
            embed.add_field(name="Thread", value=f"<#{record.thread_id}>")

        try:
            # Reuse the bot's internal aiohttp session to avoid creating a new one.
            webhook = discord.Webhook.from_url(
                self._webhook_url,
                session=self._bot.http._HTTPClient__session,  # type: ignore[attr-defined]
            )
            await webhook.send(embed=embed, username="Labmate")
        except discord.HTTPException as exc:
            log.error("webhook notification failed: %s", exc)

    # ── Generator bridge ───────────────────────────────────────────────────

    async def _generate(self, prompt: str, task_id: Optional[str] = None) -> AsyncGenerator[str, None]:
        """
        Post to Redis stream and yield result chunks as they arrive.
        Falls back to in-process orchestrator if Redis is unavailable (for testing).
        """
        # Redis path: post goal and wait for per-task result stream
        if self._redis is not None:
            redis_task_id = task_id or str(uuid.uuid4())
            result_key = f"labmate:result:{redis_task_id}"

            # Subscribe BEFORE posting the goal to avoid missing a fast-finishing task.
            pubsub = self._redis.pubsub()
            await pubsub.subscribe(result_key)
            try:
                # Canonical labmate:goals format: single "payload" field with JSON object.
                await self._redis.xadd(
                    "labmate:goals",
                    {"payload": json.dumps({
                        "task_id": redis_task_id,
                        "task": prompt,
                        "session_id": redis_task_id,
                    })},
                )

                # Poll once immediately (result may have arrived before subscribe).
                deadline = time.monotonic() + FOLLOWUP_EXPIRY_SECONDS
                raw_result = await self._redis.get(result_key)
                while raw_result is None and time.monotonic() < deadline:
                    msg = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=5.0
                    )
                    if msg is not None and msg.get("type") == "message":
                        raw_result = await self._redis.get(result_key)
            finally:
                await pubsub.unsubscribe(result_key)
                await pubsub.close()

            if raw_result is None:
                yield "⚠️ _No result received from the orchestrator (timed out)._"
                return

            if isinstance(raw_result, bytes):
                raw_result = raw_result.decode()
            result = json.loads(raw_result)

            if result.get("ok"):
                yield _render_result(result.get("state"))
            else:
                yield f"❌ Task failed: {result.get('error', 'unknown error')}"
            return
        else:
            # Fallback for tests: use in-process orchestrator if available
            if self._orchestrator and hasattr(self._orchestrator, "stream"):
                async for token in self._orchestrator.stream(prompt):
                    yield token
            elif self._orchestrator and hasattr(self._orchestrator, "run"):
                loop = asyncio.get_running_loop()
                result: str = await loop.run_in_executor(None, self._orchestrator.run, prompt)
                yield result

    # ── Gate helpers ───────────────────────────────────────────────────────

    async def _resolve_gate_task_id(self, prefix: Optional[str]) -> Optional[str]:
        """Return a full task_id that has a pending gate, or None."""
        async with self._tasks_lock:
            snapshot = dict(self._tasks)

        candidates = list(snapshot.keys())
        if prefix:
            candidates = [tid for tid in candidates if tid.startswith(prefix)]

        for tid in candidates:
            try:
                if await self._orchestrator.pending_gate(tid):
                    return tid
            except AttributeError:
                pass
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _status_color(status: str) -> discord.Color:
    return {
        "completed": discord.Color.green(),
        "failed": discord.Color.red(),
        "cancelled": discord.Color.orange(),
        "truncated": discord.Color.yellow(),
    }.get(status, discord.Color.blurple())


# ---------------------------------------------------------------------------
# __main__ entrypoint
# ---------------------------------------------------------------------------

async def _main() -> None:
    from dotenv import load_dotenv
    load_dotenv()

    logging.basicConfig(
        stream=__import__("sys").stderr,
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    token = os.environ["DISCORD_BOT_TOKEN"]
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")

    connector = DiscordConnector(
        token=token,
        orchestrator=None,  # No in-process orchestrator; uses Redis for IPC
        webhook_url=webhook_url,
    )

    await connector.start()


if __name__ == "__main__":
    import uvloop
    uvloop.run(_main())
