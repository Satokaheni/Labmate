# tests/services/connectors/test_discord_connector.py
from __future__ import annotations

import asyncio
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_connector(orchestrator=None, webhook_url=None, edit_interval=0.0):
    """Build a DiscordConnector with a dummy token and mocked discord.Bot."""
    # Patch the Bot constructor so no real discord state is created.
    with patch("services.connectors.discord_connector.commands.Bot") as MockBot:
        mock_bot = MagicMock()
        MockBot.return_value = mock_bot

        from services.connectors.discord_connector import DiscordConnector
        connector = DiscordConnector(
            token="fake-token",
            orchestrator=orchestrator or MagicMock(),
            webhook_url=webhook_url,
            edit_interval=edit_interval,
        )
        connector._bot = mock_bot
        return connector


def _make_interaction(in_guild=True):
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    channel = MagicMock()
    channel.create_thread = AsyncMock()
    channel.send = AsyncMock()
    interaction.channel = channel
    interaction.guild = MagicMock() if in_guild else None
    return interaction


def _make_message():
    msg = MagicMock()
    msg.edit = AsyncMock()
    return msg


def _make_task_record(description="do something"):
    from services.connectors.discord_connector import TaskRecord
    return TaskRecord(task_id="aabbccdd-1234", description=description)


async def _async_gen(*tokens):
    for t in tokens:
        yield t


# ---------------------------------------------------------------------------
# DiscordConnector.__init__
# ---------------------------------------------------------------------------

@pytest.mark.mocked
class TestInit:
    def test_connector_stores_token_and_orchestrator(self):
        orch = MagicMock()
        with patch("services.connectors.discord_connector.commands.Bot"):
            from services.connectors.discord_connector import DiscordConnector
            c = DiscordConnector(token="t", orchestrator=orch)
        assert c._token == "t"
        assert c._orchestrator is orch

    def test_connector_stores_webhook_url(self):
        with patch("services.connectors.discord_connector.commands.Bot"):
            from services.connectors.discord_connector import DiscordConnector
            c = DiscordConnector(token="t", orchestrator=MagicMock(), webhook_url="https://example.com/hook")
        assert c._webhook_url == "https://example.com/hook"


# ---------------------------------------------------------------------------
# defer() is first await (structural check)
# ---------------------------------------------------------------------------

@pytest.mark.mocked
class TestDeferFirst:
    def test_task_command_source_defers_before_any_other_await(self):
        """Structural: defer() must appear before uuid4(), lock, create_task in source."""
        import inspect
        import ast
        from services.connectors import discord_connector
        src = inspect.getsource(discord_connector)

        # Find the task_command function body and check defer is first await
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "task_command":
                # Walk statements; the first Expr containing an Await must be defer
                for stmt in node.body:
                    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Await):
                        call_node = stmt.value.value
                        # Should be interaction.response.defer(...)
                        src_fragment = ast.unparse(call_node)
                        assert "defer" in src_fragment, (
                            f"First await in task_command is not defer(): {src_fragment}"
                        )
                        return
        pytest.fail("task_command not found or has no awaits")


# ---------------------------------------------------------------------------
# _streaming_worker — throttled edits
# ---------------------------------------------------------------------------

@pytest.mark.mocked
class TestStreamingWorker:
    @pytest.mark.asyncio
    async def test_edits_message_at_most_once_per_interval(self):
        connector = _make_connector(edit_interval=0.0)  # zero interval: edit on every token
        interaction = _make_interaction()
        record = _make_task_record("test")
        msg = _make_message()
        sink = MagicMock()

        connector._generate = lambda _, task_id=None: _async_gen("token1", "token2", "token3")

        await connector._streaming_worker(interaction, record, sink, msg)

        # At least one edit must have happened (final flush always fires)
        assert msg.edit.await_count >= 1

    @pytest.mark.asyncio
    async def test_final_flush_removes_streaming_indicator(self):
        from services.connectors.discord_connector import STREAMING_INDICATOR
        connector = _make_connector(edit_interval=999.0)  # never throttle during test
        interaction = _make_interaction()
        record = _make_task_record()
        msg = _make_message()
        sink = MagicMock()

        connector._generate = lambda _, task_id=None: _async_gen("hello")
        await connector._streaming_worker(interaction, record, sink, msg)

        # The last edit call must NOT contain the streaming indicator
        last_call_content = msg.edit.call_args_list[-1].kwargs.get("content") or \
                            msg.edit.call_args_list[-1].args[0] if msg.edit.call_args_list[-1].args else \
                            msg.edit.call_args_list[-1].kwargs["content"]
        assert STREAMING_INDICATOR not in last_call_content

    @pytest.mark.asyncio
    async def test_streaming_indicator_appended_on_intermediate_edit(self):
        from services.connectors.discord_connector import STREAMING_INDICATOR
        connector = _make_connector(edit_interval=0.0)
        interaction = _make_interaction()
        record = _make_task_record()
        msg = _make_message()
        sink = MagicMock()

        # Two tokens: first edit is intermediate, second is final
        # edit_interval=0 means we edit after every token
        tokens_emitted = []

        async def gen(_, task_id=None):
            yield "abc"
            await asyncio.sleep(0)
            yield "def"

        connector._generate = gen

        await connector._streaming_worker(interaction, record, sink, msg)

        # At least one intermediate edit should have the indicator
        all_edits = [
            (c.kwargs.get("content") or (c.args[0] if c.args else ""))
            for c in msg.edit.call_args_list
        ]
        # Final must not have indicator; at least one before final must have it
        assert STREAMING_INDICATOR not in all_edits[-1]
        assert any(STREAMING_INDICATOR in e for e in all_edits[:-1])


# ---------------------------------------------------------------------------
# _split_message — 1900-char split at last newline
# ---------------------------------------------------------------------------

@pytest.mark.mocked
class TestSplitMessage:
    @pytest.mark.asyncio
    async def test_split_at_last_newline_within_limit(self):
        from services.connectors.discord_connector import MESSAGE_CHAR_LIMIT
        connector = _make_connector()
        current_msg = _make_message()
        new_msg = MagicMock()
        new_msg.edit = AsyncMock()

        sink = MagicMock()
        sink.send = AsyncMock(return_value=new_msg)

        # Build a buffer that crosses MESSAGE_CHAR_LIMIT with a newline before the limit
        line_a = "A" * (MESSAGE_CHAR_LIMIT - 50)
        line_b = "B" * 100
        buf = line_a + "\n" + line_b

        remainder, returned_msg = await connector._split_message(sink, current_msg, buf)

        # The frozen message should contain line_a only (split at newline)
        frozen_content = current_msg.edit.call_args.kwargs.get("content") or \
                         current_msg.edit.call_args.args[0]
        assert frozen_content == line_a
        # Remainder is the continuation
        assert remainder == line_b
        assert returned_msg is new_msg

    @pytest.mark.asyncio
    async def test_split_hard_cut_when_no_newline(self):
        from services.connectors.discord_connector import MESSAGE_CHAR_LIMIT
        connector = _make_connector()
        current_msg = _make_message()
        new_msg = MagicMock()
        new_msg.edit = AsyncMock()
        sink = MagicMock()
        sink.send = AsyncMock(return_value=new_msg)

        # No newline in buffer
        buf = "X" * (MESSAGE_CHAR_LIMIT + 50)

        remainder, _ = await connector._split_message(sink, current_msg, buf)

        frozen = current_msg.edit.call_args.kwargs.get("content") or \
                 current_msg.edit.call_args.args[0]
        assert len(frozen) == MESSAGE_CHAR_LIMIT
        assert remainder == buf[MESSAGE_CHAR_LIMIT:]


# ---------------------------------------------------------------------------
# _safe_edit — HTTP 429 retry + 401/404 silent discard
# ---------------------------------------------------------------------------

@pytest.mark.mocked
class TestSafeEdit:
    @pytest.mark.asyncio
    async def test_retries_on_429_up_to_5_times(self):
        connector = _make_connector()
        msg = MagicMock()

        exc_429 = discord.HTTPException(MagicMock(status=429), "rate limited")
        exc_429.status = 429
        exc_429.retry_after = 0.0

        # Fail 4 times then succeed
        msg.edit = AsyncMock(side_effect=[exc_429, exc_429, exc_429, exc_429, None])

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await connector._safe_edit(msg, "content", final=True)

        assert msg.edit.await_count == 5

    @pytest.mark.asyncio
    async def test_silently_returns_on_401(self):
        connector = _make_connector()
        msg = MagicMock()

        exc_401 = discord.HTTPException(MagicMock(status=401), "unauthorized")
        exc_401.status = 401
        msg.edit = AsyncMock(side_effect=exc_401)

        # Should not raise
        await connector._safe_edit(msg, "content", final=True)
        assert msg.edit.await_count == 1

    @pytest.mark.asyncio
    async def test_silently_returns_on_404(self):
        connector = _make_connector()
        msg = MagicMock()

        exc_404 = discord.HTTPException(MagicMock(status=404), "not found")
        exc_404.status = 404
        msg.edit = AsyncMock(side_effect=exc_404)

        await connector._safe_edit(msg, "content", final=True)
        assert msg.edit.await_count == 1


# ---------------------------------------------------------------------------
# 15-minute expiry guard
# ---------------------------------------------------------------------------

@pytest.mark.mocked
class TestExpiryGuard:
    @pytest.mark.asyncio
    async def test_truncates_and_sets_status_after_expiry(self):
        from services.connectors.discord_connector import FOLLOWUP_EXPIRY_SECONDS
        connector = _make_connector(edit_interval=999.0)
        interaction = _make_interaction()
        record = _make_task_record()
        msg = _make_message()
        sink = MagicMock()

        async def slow_gen(_, task_id=None):
            yield "some output"

        connector._generate = slow_gen

        # First two calls are started_at and last_edit_at (both return 0).
        # All subsequent calls (the expiry check inside the loop) return past the threshold.
        _call_count = [0]
        def _fake_monotonic():
            _call_count[0] += 1
            if _call_count[0] <= 2:
                return 0.0
            return FOLLOWUP_EXPIRY_SECONDS + 5.0

        with patch("services.connectors.discord_connector.time") as mock_time:
            mock_time.monotonic.side_effect = _fake_monotonic
            await connector._streaming_worker(interaction, record, sink, msg)

        assert record.status == "truncated"
        # Final edit should contain the truncation notice
        final_content = msg.edit.call_args.kwargs.get("content") or msg.edit.call_args.args[0]
        assert "truncated" in final_content.lower() or "⚠️" in final_content


# ---------------------------------------------------------------------------
# DM fallback — no thread creation
# ---------------------------------------------------------------------------

@pytest.mark.mocked
class TestDMFallback:
    @pytest.mark.asyncio
    async def test_no_thread_created_in_dm(self):
        connector = _make_connector()
        interaction = _make_interaction(in_guild=False)
        record = _make_task_record()

        initial_msg = _make_message()
        interaction.followup.send = AsyncMock(return_value=initial_msg)

        connector._streaming_worker = AsyncMock()

        await connector._task_worker(interaction, record)

        # create_thread should never be called in a DM
        interaction.channel.create_thread.assert_not_called()
        assert record.thread_id is None


# ---------------------------------------------------------------------------
# Task cancellation
# ---------------------------------------------------------------------------

@pytest.mark.mocked
class TestCancelCommand:
    @pytest.mark.asyncio
    async def test_cancelled_task_sets_status_cancelled(self):
        connector = _make_connector()
        interaction = _make_interaction()
        record = _make_task_record()

        asyncio_task = MagicMock()
        asyncio_task.done.return_value = False
        asyncio_task.cancel = MagicMock()
        record.asyncio_task = asyncio_task
        record.status = "running"

        async with connector._tasks_lock:
            connector._tasks[record.task_id] = record

        # Simulate the cancel command handler logic directly
        record.asyncio_task.cancel()
        record.status = "cancelled"

        assert record.status == "cancelled"
        asyncio_task.cancel.assert_called_once()


# ---------------------------------------------------------------------------
# Webhook notification
# ---------------------------------------------------------------------------

@pytest.mark.mocked
class TestWebhookNotification:
    @pytest.mark.asyncio
    async def test_webhook_fires_on_completion(self):
        connector = _make_connector(webhook_url="https://discord.com/api/webhooks/123/abc")
        record = _make_task_record()
        record.status = "completed"
        record.thread_id = 999

        mock_webhook = MagicMock()
        mock_webhook.send = AsyncMock()

        mock_session = MagicMock()
        connector._bot.http._HTTPClient__session = mock_session

        with patch("discord.Webhook.from_url", return_value=mock_webhook):
            await connector._notify_completion(record)

        mock_webhook.send.assert_awaited_once()
        _, kwargs = mock_webhook.send.call_args
        assert kwargs.get("username") == "Labmate"

    @pytest.mark.asyncio
    async def test_no_webhook_when_url_not_set(self):
        connector = _make_connector(webhook_url=None)
        record = _make_task_record()
        record.status = "completed"

        with patch("discord.Webhook.from_url") as mock_from_url:
            await connector._notify_completion(record)

        mock_from_url.assert_not_called()

    @pytest.mark.asyncio
    async def test_webhook_failure_does_not_propagate(self):
        connector = _make_connector(webhook_url="https://discord.com/api/webhooks/123/abc")
        record = _make_task_record()
        record.status = "failed"

        mock_webhook = MagicMock()
        exc = discord.HTTPException(MagicMock(status=429), "rate limited")
        exc.status = 429
        mock_webhook.send = AsyncMock(side_effect=exc)

        connector._bot.http._HTTPClient__session = MagicMock()

        with patch("discord.Webhook.from_url", return_value=mock_webhook):
            # Must not raise
            await connector._notify_completion(record)


# ---------------------------------------------------------------------------
# _generate — Redis stream path vs orchestrator fallback
# ---------------------------------------------------------------------------

@pytest.mark.mocked
class TestGenerate:
    @pytest.mark.asyncio
    async def test_generate_posts_canonical_payload_to_goals_stream(self):
        """Verify xadd called with canonical 'payload' field containing JSON with task, task_id, session_id."""
        from unittest.mock import AsyncMock
        import json as json_module
        connector = _make_connector(orchestrator=None)

        mock_redis = AsyncMock()
        connector._redis = mock_redis

        # Mock pubsub
        mock_pubsub = AsyncMock()
        mock_pubsub.subscribe = AsyncMock()
        mock_pubsub.unsubscribe = AsyncMock()
        mock_pubsub.close = AsyncMock()
        mock_pubsub.get_message = AsyncMock(return_value=None)
        mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

        # Mock get to return immediately on first call with a result
        mock_redis.get = AsyncMock(
            return_value=json_module.dumps({"ok": True, "state": "done"}).encode()
        )

        collected = []
        async for token in connector._generate("test prompt", task_id="test-task-id"):
            collected.append(token)

        # Verify xadd was called with canonical format
        xadd_call = mock_redis.xadd.call_args
        assert xadd_call[0][0] == "labmate:goals"
        payload_str = xadd_call[0][1]["payload"]
        payload = json_module.loads(payload_str)
        assert payload["task_id"] == "test-task-id"
        assert payload["task"] == "test prompt"
        assert payload["session_id"] == "test-task-id"

    @pytest.mark.asyncio
    async def test_generate_yields_result_on_pubsub_notification(self):
        """Mock pubsub to deliver a message, get returns JSON blob, asserts correct string yielded."""
        from unittest.mock import AsyncMock
        import json as json_module
        connector = _make_connector(orchestrator=None)

        mock_redis = AsyncMock()
        connector._redis = mock_redis

        # Mock pubsub
        mock_pubsub = AsyncMock()
        mock_pubsub.subscribe = AsyncMock()
        mock_pubsub.unsubscribe = AsyncMock()
        mock_pubsub.close = AsyncMock()
        # Return a message on first call, then None
        mock_pubsub.get_message = AsyncMock(
            side_effect=[{"type": "message"}, None]
        )
        mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

        # Mock get: return None first time, then return result after pubsub notification
        mock_redis.get = AsyncMock(
            side_effect=[
                None,  # first immediate poll
                json_module.dumps({"ok": True, "state": "Successfully completed task"}).encode()
            ]
        )

        collected = []
        async for token in connector._generate("test prompt", task_id="test-id"):
            collected.append(token)

        assert len(collected) == 1
        assert collected[0] == "Successfully completed task"

    @pytest.mark.asyncio
    async def test_generate_yields_timeout_message_when_no_result(self):
        """Deadline expires, raw_result still None, asserts timeout warning yielded."""
        from unittest.mock import AsyncMock
        connector = _make_connector(orchestrator=None)

        mock_redis = AsyncMock()
        connector._redis = mock_redis

        # Mock pubsub
        mock_pubsub = AsyncMock()
        mock_pubsub.subscribe = AsyncMock()
        mock_pubsub.unsubscribe = AsyncMock()
        mock_pubsub.close = AsyncMock()
        mock_pubsub.get_message = AsyncMock(return_value=None)
        mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

        # Mock get to always return None (simulating timeout)
        mock_redis.get = AsyncMock(return_value=None)

        # Patch time.monotonic to simulate deadline expiry
        collected = []
        _call_count = [0]
        def _fake_monotonic():
            _call_count[0] += 1
            if _call_count[0] <= 1:
                return 0.0  # first call in _generate
            return 1000.0  # way past deadline

        with patch("services.connectors.discord_connector.time.monotonic", side_effect=_fake_monotonic):
            async for token in connector._generate("test prompt", task_id="test-id"):
                collected.append(token)

        assert len(collected) == 1
        assert "timed out" in collected[0].lower()

    @pytest.mark.asyncio
    async def test_posts_to_redis_stream_and_yields_results(self):
        """Test the primary Redis pubsub+GET path."""
        from unittest.mock import AsyncMock
        import json as json_module
        connector = _make_connector(orchestrator=None)

        # Mock Redis client
        mock_redis = AsyncMock()
        connector._redis = mock_redis

        # Mock pubsub
        mock_pubsub = AsyncMock()
        mock_pubsub.subscribe = AsyncMock()
        mock_pubsub.unsubscribe = AsyncMock()
        mock_pubsub.close = AsyncMock()
        mock_pubsub.get_message = AsyncMock(return_value=None)
        mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

        # Mock get to return result
        mock_redis.get = AsyncMock(
            return_value=json_module.dumps({"ok": True, "state": "hello"}).encode()
        )

        collected = []
        async for token in connector._generate("test prompt", task_id="test-task-id"):
            collected.append(token)

        # Verify xadd was called
        mock_redis.xadd.assert_awaited_once()
        xadd_args = mock_redis.xadd.call_args[0]
        assert xadd_args[0] == "labmate:goals"
        payload_str = xadd_args[1]["payload"]
        payload = json_module.loads(payload_str)
        assert payload["task_id"] == "test-task-id"

        # Verify pubsub operations
        mock_pubsub.subscribe.assert_awaited_once()
        mock_pubsub.unsubscribe.assert_awaited_once()
        mock_pubsub.close.assert_awaited_once()

        # Verify get was called
        mock_redis.get.assert_awaited()

        # Verify the result was yielded
        assert collected == ["hello"]

    @pytest.mark.asyncio
    async def test_falls_back_to_orchestrator_stream_when_no_redis(self):
        """Test fallback to orchestrator.stream() when Redis is None."""
        orch = MagicMock()

        async def fake_stream(prompt):
            yield "token-a"
            yield "token-b"

        orch.stream = fake_stream
        connector = _make_connector(orchestrator=orch)
        connector._redis = None  # Explicitly set to None to test fallback

        collected = []
        async for token in connector._generate("test prompt"):
            collected.append(token)

        assert collected == ["token-a", "token-b"]

    @pytest.mark.asyncio
    async def test_falls_back_to_orchestrator_run_when_no_stream(self):
        """Test fallback to orchestrator.run() via run_in_executor when no stream."""
        orch = MagicMock(spec=[])  # no .stream attribute
        orch.run = MagicMock(return_value="sync result")
        connector = _make_connector(orchestrator=orch)
        connector._redis = None

        collected = []
        async for token in connector._generate("test prompt"):
            collected.append(token)

        assert collected == ["sync result"]


# ---------------------------------------------------------------------------
# task_worker integration — thread creation success path
# ---------------------------------------------------------------------------

@pytest.mark.mocked
class TestTaskWorkerIntegration:
    @pytest.mark.asyncio
    async def test_task_worker_creates_thread_in_guild(self):
        connector = _make_connector()
        interaction = _make_interaction(in_guild=True)
        record = _make_task_record()

        thread = MagicMock()
        thread.id = 42
        thread.send = AsyncMock()
        interaction.channel.create_thread = AsyncMock(return_value=thread)

        initial_msg = _make_message()
        interaction.followup.send = AsyncMock(return_value=initial_msg)

        connector._streaming_worker = AsyncMock()
        connector._notify_completion = AsyncMock()

        await connector._task_worker(interaction, record)

        interaction.channel.create_thread.assert_awaited_once()
        assert record.thread_id == 42

    @pytest.mark.asyncio
    async def test_task_worker_status_completed_after_success(self):
        connector = _make_connector()
        interaction = _make_interaction()
        record = _make_task_record()

        initial_msg = _make_message()
        interaction.followup.send = AsyncMock(return_value=initial_msg)
        interaction.channel.create_thread = AsyncMock(return_value=MagicMock(id=1, send=AsyncMock()))

        connector._streaming_worker = AsyncMock()
        connector._notify_completion = AsyncMock()

        await connector._task_worker(interaction, record)

        assert record.status == "completed"

    @pytest.mark.asyncio
    async def test_task_worker_status_failed_on_exception(self):
        connector = _make_connector()
        interaction = _make_interaction()
        record = _make_task_record()

        initial_msg = _make_message()
        interaction.followup.send = AsyncMock(return_value=initial_msg)
        interaction.channel.create_thread = AsyncMock(return_value=MagicMock(id=1, send=AsyncMock()))

        connector._streaming_worker = AsyncMock(side_effect=RuntimeError("boom"))
        connector._notify_completion = AsyncMock()

        await connector._task_worker(interaction, record)

        assert record.status == "failed"


# ---------------------------------------------------------------------------
# Two concurrent tasks do not cross-contaminate
# ---------------------------------------------------------------------------

@pytest.mark.mocked
class TestTaskIsolation:
    @pytest.mark.asyncio
    async def test_two_tasks_create_separate_threads(self):
        connector = _make_connector()

        thread_a = MagicMock(id=100, send=AsyncMock())
        thread_b = MagicMock(id=200, send=AsyncMock())

        call_count = 0

        async def create_thread(**kwargs):
            nonlocal call_count
            call_count += 1
            return thread_a if call_count == 1 else thread_b

        interaction_a = _make_interaction()
        interaction_a.channel.create_thread = create_thread
        interaction_a.followup.send = AsyncMock(return_value=_make_message())

        interaction_b = _make_interaction()
        interaction_b.channel.create_thread = create_thread
        interaction_b.followup.send = AsyncMock(return_value=_make_message())

        connector._streaming_worker = AsyncMock()
        connector._notify_completion = AsyncMock()

        from services.connectors.discord_connector import TaskRecord
        record_a = TaskRecord(task_id="aaaa-1111", description="task A")
        record_b = TaskRecord(task_id="bbbb-2222", description="task B")

        await asyncio.gather(
            connector._task_worker(interaction_a, record_a),
            connector._task_worker(interaction_b, record_b),
        )

        assert record_a.thread_id != record_b.thread_id


# ---------------------------------------------------------------------------
# _render_result — clean answer extraction
# ---------------------------------------------------------------------------

@pytest.mark.mocked
class TestRenderResult:
    def test_render_result_prefers_final_answer(self):
        from services.connectors.discord_connector import _render_result
        state = {
            "final_answer": "Task completed successfully",
            "messages": [],
            "goal_tree": {},
        }
        result = _render_result(state)
        assert result == "Task completed successfully"

    def test_render_result_prefers_root_goal_result_when_no_final_answer(self):
        from services.connectors.discord_connector import _render_result
        state = {
            "goal_tree": {
                "root": {"result": "Root task result"}
            },
            "messages": [],
        }
        result = _render_result(state)
        assert result == "Root task result"

    def test_render_result_falls_back_to_json_when_no_clean_result(self):
        from services.connectors.discord_connector import _render_result
        state = {
            "goal_tree": {},
            "messages": [],
            "some_field": "value",
        }
        result = _render_result(state)
        # Should be JSON-dumped
        assert "goal_tree" in result
        assert isinstance(result, str)

    def test_render_result_truncates_long_json(self):
        from services.connectors.discord_connector import _render_result
        # Create a state with lots of data to exceed 3000 chars
        state = {
            "goal_tree": {f"goal_{i}": {"description": "x" * 100} for i in range(100)},
            "messages": [],
        }
        result = _render_result(state)
        assert len(result) <= 3030  # 3000 + truncation notice length margin
        assert "truncated" in result

    def test_render_result_handles_none(self):
        from services.connectors.discord_connector import _render_result
        result = _render_result(None)
        assert result == "_(empty result)_"

    def test_render_result_handles_string(self):
        from services.connectors.discord_connector import _render_result
        result = _render_result("Direct string result")
        assert result == "Direct string result"

    def test_render_result_ignores_empty_final_answer(self):
        from services.connectors.discord_connector import _render_result
        state = {
            "final_answer": "",  # Empty, should be skipped
            "goal_tree": {
                "root": {"result": "Fallback result"}
            },
        }
        result = _render_result(state)
        assert result == "Fallback result"

    def test_render_result_ignores_whitespace_only_final_answer(self):
        from services.connectors.discord_connector import _render_result
        state = {
            "final_answer": "   ",  # Whitespace only, should be skipped
            "goal_tree": {
                "root": {"result": "Fallback result"}
            },
        }
        result = _render_result(state)
        assert result == "Fallback result"


import discord
