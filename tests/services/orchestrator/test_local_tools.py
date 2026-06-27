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


async def test_request_local_tool_raises_on_error_frame(redis):
    task_id = "task-err"
    emitter = events.EventEmitter(redis, task_id)
    token = events.current_emitter.set(emitter)
    try:
        async def fake_client_with_error() -> None:
            ev_stream = f"{events.EVENTS_STREAM_PREFIX}{task_id}"
            for _ in range(50):
                resp = await redis.xread({ev_stream: "0"}, count=10, block=100)
                if not resp:
                    continue
                for _s, entries in resp:
                    for _eid, fields in entries:
                        ev = json.loads(fields["event"])
                        if ev.get("type") == "tool.request":
                            await redis.xadd(
                                f"{TOOL_RESULTS_PREFIX}{task_id}",
                                {
                                    "result": json.dumps(
                                        {
                                            "tool_request_id": ev["tool_request_id"],
                                            "result": None,
                                            "error": "permission denied",
                                        }
                                    )
                                },
                            )
                            return

        client_task = asyncio.create_task(fake_client_with_error())
        with pytest.raises(RuntimeError, match="permission denied"):
            await request_local_tool(redis, "read_file", {"path": "x"}, timeout=5.0)
        await client_task
    finally:
        events.current_emitter.reset(token)


async def test_write_tool_result_xadds_frame_to_results_stream(redis):
    from services.orchestrator.local_tools import write_tool_result
    await write_tool_result(redis, "task-w1", "req-42", {"ok": True})
    entries = await redis.xrange(f"{TOOL_RESULTS_PREFIX}task-w1")
    assert len(entries) == 1
    _id, fields = entries[0]
    frame = json.loads(fields["result"])
    assert frame == {"tool_request_id": "req-42", "result": {"ok": True}, "error": None}


async def test_write_tool_result_with_error(redis):
    from services.orchestrator.local_tools import write_tool_result
    await write_tool_result(redis, "task-w2", "req-99", None, error="path escape")
    entries = await redis.xrange(f"{TOOL_RESULTS_PREFIX}task-w2")
    _id, fields = entries[0]
    frame = json.loads(fields["result"])
    assert frame["error"] == "path escape"
    assert frame["result"] is None


# ── write_file read-back verification ────────────────────────────────────────
from services.orchestrator.local_tools import verify_written_content  # noqa: E402


def test_verify_written_content_returns_none_on_exact_match():
    assert verify_written_content("hello\nworld\n", "hello\nworld\n") is None


def test_verify_written_content_flags_mismatch():
    err = verify_written_content("NEW CONTENT", "OLD CONTENT")
    assert err is not None
    assert err.startswith("write verification failed")
    assert "did not match" in err


def test_verify_written_content_flags_partial_write():
    err = verify_written_content("line1\nline2\n", "line1\n")
    assert err is not None
    assert "did not match" in err


def test_verify_written_content_unwraps_content_dict_on_match():
    # read_file (execute_local_tool / A/B responder) returns {"content": str};
    # an exact match in that shape must verify as applied, not be flagged.
    assert verify_written_content("hello\nworld\n", {"content": "hello\nworld\n"}) is None


def test_verify_written_content_unwraps_content_dict_on_mismatch():
    err = verify_written_content("NEW", {"content": "OLD"})
    assert err is not None
    assert "did not match" in err


def test_verify_written_content_treats_non_string_readback_as_mismatch():
    err = verify_written_content("content", {"unexpected": "shape"})
    assert err is not None
    assert "did not match" in err


def test_verify_written_content_treats_none_readback_as_mismatch():
    err = verify_written_content("content", None)
    assert err is not None
    assert "did not match" in err


# ── run_tests timeout clamping ──────────────────────────────────────────────
from services.orchestrator.local_tools import (
    build_run_tests_command,
    RUN_TESTS_TIMEOUT_MS_MAX,
)


def test_build_run_tests_command_clamps_explicit_timeout_to_cap():
    cmd, timeout_ms = build_run_tests_command({"timeout_ms": 120000})
    assert cmd == "pytest"
    assert timeout_ms == RUN_TESTS_TIMEOUT_MS_MAX == 60000


def test_build_run_tests_command_default_timeout_within_cap():
    cmd, timeout_ms = build_run_tests_command({})
    assert timeout_ms <= RUN_TESTS_TIMEOUT_MS_MAX


def test_build_run_tests_command_small_timeout_unchanged():
    _, timeout_ms = build_run_tests_command({"timeout_ms": 5000})
    assert timeout_ms == 5000


# ── sandbox test helpers (Task 3 helpers) ──────────────────────────────────────
from services.orchestrator.local_tools import (
    build_sandbox_test_args,
    shape_sandbox_test_result,
    SANDBOX_TEST_TIMEOUT_S_MAX,
)


def test_build_sandbox_test_args_defaults_to_workspace_root():
    a = build_sandbox_test_args({}, "/workspace/proj")
    assert a["test_path"] == "/workspace/proj"
    assert a["framework"] == "pytest"
    assert a["timeout"] == SANDBOX_TEST_TIMEOUT_S_MAX  # 120000ms default -> 120s, clamped


def test_build_sandbox_test_args_resolves_relative_path_against_workspace():
    a = build_sandbox_test_args({"path": "tests/test_x.py"}, "/workspace/proj")
    assert a["test_path"] == "/workspace/proj/tests/test_x.py"


def test_build_sandbox_test_args_keeps_absolute_path():
    a = build_sandbox_test_args({"path": "/abs/test_x.py"}, "/workspace/proj")
    assert a["test_path"] == "/abs/test_x.py"


def test_build_sandbox_test_args_converts_and_clamps_timeout():
    a = build_sandbox_test_args({"timeout_ms": 300000}, "/w")
    assert a["timeout"] == SANDBOX_TEST_TIMEOUT_S_MAX  # 300s clamped to 120s
    b = build_sandbox_test_args({"timeout_ms": 5000}, "/w")
    assert b["timeout"] == 5  # 5000ms -> 5s


def test_build_sandbox_test_args_forwards_expr():
    a = build_sandbox_test_args({"expr": "alpha or beta"}, "/w")
    assert a["expr"] == "alpha or beta"
    assert "expr" not in build_sandbox_test_args({}, "/w")


def test_shape_sandbox_test_result_passing():
    envelope = {
        "ok": True,
        "result": {
            "content": [{"type": "text", "text":
                '{"passed": 3, "failed": 0, "errors": 0, "output": "3 passed", "timed_out": false}'}],
            "isError": False,
        },
    }
    out = shape_sandbox_test_result(envelope)
    assert out == {"ok": True, "exit_code": 0, "raw_output": "3 passed"}


def test_shape_sandbox_test_result_failing():
    envelope = {
        "ok": True,
        "result": {
            "content": [{"type": "text", "text":
                '{"passed": 1, "failed": 2, "errors": 0, "output": "FAILED test_x", "timed_out": false}'}],
            "isError": True,
        },
    }
    out = shape_sandbox_test_result(envelope)
    assert out["ok"] is False
    assert out["exit_code"] == 1
    assert "FAILED" in out["raw_output"]


def test_shape_sandbox_test_result_infra_error():
    out = shape_sandbox_test_result({"ok": False, "error": "skill_unavailable", "detail": "no tool"})
    assert out["ok"] is False
    assert out["exit_code"] == 1
    assert "skill_unavailable" in out["raw_output"]


def test_shape_sandbox_test_result_timed_out():
    envelope = {
        "ok": True,
        "result": {"content": [{"type": "text", "text":
            '{"passed": 0, "failed": 0, "errors": 0, "output": "", "timed_out": true}'}]},
    }
    out = shape_sandbox_test_result(envelope)
    assert out["ok"] is False
