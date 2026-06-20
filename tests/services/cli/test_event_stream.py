import json
import pytest
from unittest.mock import AsyncMock, MagicMock
from services.cli import event_stream


@pytest.mark.asyncio
async def test_tail_events_decodes_and_advances(monkeypatch):
    batch = [["labmate:events:t1", [("1-0", {"event": json.dumps({"type": "tool.start", "seq": 1})})]]]
    calls = [batch, []]
    fake = MagicMock()
    fake.xread = AsyncMock(side_effect=lambda *a, **k: calls.pop(0) if calls else [])
    monkeypatch.setattr(event_stream.aioredis, "from_url", lambda *a, **k: fake)

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
