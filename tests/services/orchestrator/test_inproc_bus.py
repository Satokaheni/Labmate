"""Async unit tests for services.orchestrator.inproc_bus primitives."""

import asyncio

import pytest

from services.orchestrator.inproc_bus import EventBus, ResultRegistry, SignalRegistry

# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


async def test_event_bus_fan_out_multiple_subscribers_in_order():
    bus = EventBus()
    sub1 = bus.subscribe("topic-a")
    sub2 = bus.subscribe("topic-a")

    bus.publish("topic-a", {"n": 1})
    bus.publish("topic-a", {"n": 2})

    for sub in (sub1, sub2):
        frame1 = await asyncio.wait_for(sub.__anext__(), timeout=1)
        frame2 = await asyncio.wait_for(sub.__anext__(), timeout=1)
        assert frame1 == {"n": 1}
        assert frame2 == {"n": 2}

    sub1.close()
    sub2.close()


async def test_event_bus_topic_isolation():
    bus = EventBus()
    sub_a = bus.subscribe("topic-a")
    sub_b = bus.subscribe("topic-b")

    bus.publish("topic-b", {"only": "b"})

    frame = await asyncio.wait_for(sub_b.__anext__(), timeout=1)
    assert frame == {"only": "b"}

    # topic-a subscriber must not receive anything published to topic-b.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sub_a.__anext__(), timeout=0.05)

    sub_a.close()
    sub_b.close()


async def test_event_bus_post_subscribe_only_no_history():
    bus = EventBus()
    # Publish before anyone subscribes: should be a no-op, not buffered.
    bus.publish("topic-a", {"before": True})

    sub = bus.subscribe("topic-a")
    bus.publish("topic-a", {"after": True})

    frame = await asyncio.wait_for(sub.__anext__(), timeout=1)
    assert frame == {"after": True}

    # Confirm the "before" frame never arrives.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sub.__anext__(), timeout=0.05)

    sub.close()


async def test_event_bus_publish_with_no_subscribers_does_not_raise():
    bus = EventBus()
    # No subscribers at all for this topic.
    bus.publish("nobody-listening", {"x": 1})  # must not raise


async def test_event_bus_close_unsubscribes_and_ends_iteration():
    bus = EventBus()
    sub = bus.subscribe("topic-a")

    async def consume():
        frames = []
        async for frame in sub:
            frames.append(frame)
        return frames

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let the consumer start waiting on the queue

    sub.close()

    frames = await asyncio.wait_for(task, timeout=1)
    assert frames == []

    # Bus no longer holds the subscriber; a subsequent publish must not error.
    bus.publish("topic-a", {"after-close": True})
    assert "topic-a" not in bus._topics


async def test_event_bus_cancellation_removes_subscriber():
    bus = EventBus()
    sub = bus.subscribe("topic-a")

    async def consume_forever():
        async for _ in sub:
            pass

    task = asyncio.create_task(consume_forever())
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Explicitly close after cancellation (consumer's responsibility, e.g. in
    # a finally block) and verify the bus no longer references the queue.
    sub.close()
    assert "topic-a" not in bus._topics


async def test_event_bus_async_context_manager_closes_on_exit():
    bus = EventBus()
    async with bus.subscribe("topic-a") as sub:
        bus.publish("topic-a", {"n": 1})
        frame = await asyncio.wait_for(sub.__anext__(), timeout=1)
        assert frame == {"n": 1}

    assert "topic-a" not in bus._topics


# ---------------------------------------------------------------------------
# SignalRegistry
# ---------------------------------------------------------------------------


def test_signal_registry_cancel_default_false_then_true_then_cleared():
    registry = SignalRegistry()
    assert registry.is_cancelled("task-1") is False

    registry.request_cancel("task-1")
    assert registry.is_cancelled("task-1") is True

    registry.clear_task("task-1")
    assert registry.is_cancelled("task-1") is False


def test_signal_registry_steer_consume_once_and_latest_write_wins():
    registry = SignalRegistry()

    assert registry.read_and_clear_steer("task-1") is None

    registry.write_steer("task-1", "first")
    registry.write_steer("task-1", "second")  # latest write wins

    assert registry.read_and_clear_steer("task-1") == "second"
    # Consume-once: second read returns None.
    assert registry.read_and_clear_steer("task-1") is None


def test_signal_registry_clear_task_resets_steer_too():
    registry = SignalRegistry()
    registry.write_steer("task-1", "pending")
    registry.clear_task("task-1")
    assert registry.read_and_clear_steer("task-1") is None


# ---------------------------------------------------------------------------
# ResultRegistry
# ---------------------------------------------------------------------------


async def test_result_registry_set_then_wait_returns_immediately():
    registry = ResultRegistry()
    registry.set_result("task-1", {"ok": True})

    result = await asyncio.wait_for(registry.wait_result("task-1"), timeout=1)
    assert result == {"ok": True}


async def test_result_registry_wait_then_set_resolves_waiter():
    registry = ResultRegistry()

    wait_task = asyncio.create_task(registry.wait_result("task-1"))
    await asyncio.sleep(0)  # let wait_task start waiting

    registry.set_result("task-1", {"ok": True})

    result = await asyncio.wait_for(wait_task, timeout=1)
    assert result == {"ok": True}


async def test_result_registry_wait_result_timeout_raises():
    registry = ResultRegistry()

    with pytest.raises(asyncio.TimeoutError):
        await registry.wait_result("task-1", timeout=0.05)


async def test_result_registry_clear_task_drops_stored_result():
    registry = ResultRegistry()
    registry.set_result("task-1", {"ok": True})
    registry.clear_task("task-1")

    with pytest.raises(asyncio.TimeoutError):
        await registry.wait_result("task-1", timeout=0.05)


@pytest.mark.asyncio
async def test_close_drains_buffered_frames_then_stops():
    """close() must not drop frames already delivered to the subscriber:
    buffered frames drain first, then iteration stops (turn.done tail safety)."""
    bus = EventBus()
    sub = bus.subscribe("events:t")
    bus.publish("events:t", {"seq": 1})
    bus.publish("events:t", {"seq": 2})
    sub.close()  # close with 2 frames still buffered
    got = [frame async for frame in sub]
    assert [f["seq"] for f in got] == [1, 2]


def test_result_registry_set_and_read_across_loops():
    """A result set in one event loop must be readable from a DIFFERENT loop.

    Guards the CI regression (Python < 3.12 raises "future belongs to a different
    loop" when a loop-bound future is awaited from another loop). ResultRegistry
    stores the value loop-agnostically, so wait_result returns it directly. This
    mirrors the BDD pattern: set via _handle in one run_until_complete loop, read
    via wait_result in another.
    """
    import asyncio as _asyncio

    reg = ResultRegistry()

    loop_a = _asyncio.new_event_loop()
    try:
        loop_a.run_until_complete(_set(reg, "t1", {"ok": True}))
    finally:
        loop_a.close()

    loop_b = _asyncio.new_event_loop()
    try:
        got = loop_b.run_until_complete(reg.wait_result("t1", timeout=1.0))
    finally:
        loop_b.close()
    assert got == {"ok": True}


async def _set(reg, task_id, result):
    reg.set_result(task_id, result)
