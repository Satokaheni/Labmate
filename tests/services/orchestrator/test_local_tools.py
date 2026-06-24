# tests/services/orchestrator/test_local_tools.py
from __future__ import annotations

import asyncio
import json

import fakeredis.aioredis
import pytest

from services.orchestrator import events
from services.orchestrator.local_tools import (
    LOCAL_TOOL_NAMES,
    TOOL_RESULTS_PREFIX,
    request_local_tool,
)


@pytest.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


def test_local_tool_names_are_the_three_file_tools():
    assert LOCAL_TOOL_NAMES == {"read_file", "write_file", "list_dir"}


async def test_request_local_tool_emits_event_and_returns_result(redis):
    task_id = "task-abc"
    emitter = events.EventEmitter(redis, task_id)
    token = events.current_emitter.set(emitter)
    try:
        # Simulate the local client: once a tool.request is on the event stream,
        # write a matching tool.result onto the results stream.
        async def fake_client() -> None:
            ev_stream = f"{events.EVENTS_STREAM_PREFIX}{task_id}"
            cur = "0"
            for _ in range(50):
                resp = await redis.xread({ev_stream: cur}, count=10, block=100)
                if not resp:
                    continue
                for _s, entries in resp:
                    for eid, fields in entries:
                        cur = eid
                        ev = json.loads(fields["event"])
                        if ev.get("type") == "tool.request":
                            await redis.xadd(
                                f"{TOOL_RESULTS_PREFIX}{task_id}",
                                {
                                    "result": json.dumps(
                                        {
                                            "tool_request_id": ev["tool_request_id"],
                                            "result": {"content": "hello"},
                                            "error": None,
                                        }
                                    )
                                },
                            )
                            return

        client_task = asyncio.create_task(fake_client())
        out = await request_local_tool(
            redis, "read_file", {"path": "notes.txt"}, timeout=5.0
        )
        await client_task
        assert out == {"content": "hello"}

        # The tool.request event was emitted with the expected shape.
        entries = await redis.xrange(f"{events.EVENTS_STREAM_PREFIX}{task_id}")
        reqs = [
            json.loads(f["event"])
            for _id, f in entries
            if json.loads(f["event"]).get("type") == "tool.request"
        ]
        assert len(reqs) == 1
        assert reqs[0]["name"] == "read_file"
        assert reqs[0]["args"] == {"path": "notes.txt"}
        assert reqs[0]["task_id"] == task_id
        assert "tool_request_id" in reqs[0]
    finally:
        events.current_emitter.reset(token)


async def test_request_local_tool_times_out_when_no_result(redis):
    task_id = "task-timeout"
    emitter = events.EventEmitter(redis, task_id)
    token = events.current_emitter.set(emitter)
    try:
        with pytest.raises(TimeoutError):
            await request_local_tool(
                redis, "read_file", {"path": "x"}, timeout=0.3
            )
    finally:
        events.current_emitter.reset(token)


async def test_request_local_tool_matches_only_its_own_request_id(redis):
    task_id = "task-mux"
    emitter = events.EventEmitter(redis, task_id)
    token = events.current_emitter.set(emitter)
    try:
        # Pre-seed a stale result for a DIFFERENT request id; it must be skipped.
        await redis.xadd(
            f"{TOOL_RESULTS_PREFIX}{task_id}",
            {"result": json.dumps({"tool_request_id": "other", "result": 1, "error": None})},
        )

        async def fake_client() -> None:
            ev_stream = f"{events.EVENTS_STREAM_PREFIX}{task_id}"
            resp = await redis.xread({ev_stream: "0"}, count=10, block=500)
            for _s, entries in resp:
                for _eid, fields in entries:
                    ev = json.loads(fields["event"])
                    if ev.get("type") == "tool.request":
                        await redis.xadd(
                            f"{TOOL_RESULTS_PREFIX}{task_id}",
                            {
                                "result": json.dumps(
                                    {"tool_request_id": ev["tool_request_id"], "result": 2, "error": None}
                                )
                            },
                        )
                        return

        client_task = asyncio.create_task(fake_client())
        out = await request_local_tool(redis, "list_dir", {"path": "."}, timeout=5.0)
        await client_task
        assert out == 2
    finally:
        events.current_emitter.reset(token)
