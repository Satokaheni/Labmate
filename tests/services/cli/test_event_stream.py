import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from services.cli import event_stream
from services.cli import redis_event_stream


@pytest.mark.asyncio
async def test_tail_events_decodes_and_advances(monkeypatch):
    batch = [["labmate:events:t1", [("1-0", {"event": json.dumps({"type": "tool.start", "seq": 1})})]]]
    calls = [batch, []]
    fake = MagicMock()
    fake.xread = AsyncMock(side_effect=lambda *a, **k: calls.pop(0) if calls else [])
    monkeypatch.setattr(redis_event_stream.aioredis, "from_url", lambda *a, **k: fake)

    got = []
    async for evt in event_stream.tail_events("redis://x", "t1"):
        got.append(evt)
        if evt.get("type") == "tool.start":
            break
    assert got[0]["type"] == "tool.start" and got[0]["seq"] == 1


def test_event_channel_constant_and_helper():
    from services.cli.event_stream import EVENTS_PREFIX, event_channel
    assert EVENTS_PREFIX == "labmate:events:"
    assert event_channel("t-1") == "labmate:events:t-1"


import asyncio
from unittest.mock import patch


async def _fake_gen(events):
    for e in events:
        yield e


@pytest.mark.asyncio
async def test_first_returns_parsed_event():
    evs = [{"type": "turn.start", "task": "hi", "seq": 1}]
    with patch("services.cli.redis_event_stream.tail_events", return_value=_fake_gen(evs)):
        from services.cli.event_stream import EventStream
        stream = EventStream("redis://localhost:6379/0", "t-1")
        ev = await stream.first(timeout=1.0)
    assert ev is not None
    assert ev["type"] == "turn.start"


@pytest.mark.asyncio
async def test_first_returns_none_when_generator_empty():
    with patch("services.cli.redis_event_stream.tail_events", return_value=_fake_gen([])):
        from services.cli.event_stream import EventStream
        stream = EventStream("redis://localhost:6379/0", "t-1")
        ev = await stream.first(timeout=0.2)
    assert ev is None


@pytest.mark.asyncio
async def test_first_returns_none_on_timeout():
    async def _blocking_gen():
        await asyncio.Event().wait()  # blocks forever
        yield {}  # never reached

    with patch("services.cli.redis_event_stream.tail_events", return_value=_blocking_gen()):
        from services.cli.event_stream import EventStream
        stream = EventStream("redis://localhost:6379/0", "t-1")
        ev = await stream.first(timeout=0.05)
        await stream.aclose()
    assert ev is None


@pytest.mark.asyncio
async def test_events_replays_first_then_continues_until_turn_done():
    evs = [
        {"type": "turn.start", "task": "hi", "seq": 0},
        {"type": "answer.delta", "text": "hi", "seq": 1},
        {"type": "turn.done", "status": "complete", "seq": 2},
        {"type": "answer.delta", "text": "AFTER", "seq": 3},  # must NOT appear
    ]
    with patch("services.cli.redis_event_stream.tail_events", return_value=_fake_gen(evs)):
        from services.cli.event_stream import EventStream
        stream = EventStream("redis://localhost:6379/0", "t-1")
        first = await stream.first(timeout=1.0)
        assert first["type"] == "turn.start"
        seen = [ev async for ev in stream.events()]
    types = [e["type"] for e in seen]
    assert types == ["turn.start", "answer.delta", "turn.done"]


@pytest.mark.asyncio
async def test_aclose_closes_generator():
    closed = []

    async def _closeable_gen():
        try:
            yield {"type": "turn.start", "seq": 0}
            await asyncio.sleep(100)
        except GeneratorExit:
            closed.append(True)

    with patch("services.cli.redis_event_stream.tail_events", return_value=_closeable_gen()):
        from services.cli.event_stream import EventStream
        stream = EventStream("redis://localhost:6379/0", "t-1")
        await stream.first(timeout=1.0)
        await stream.aclose()
    assert closed == [True]
