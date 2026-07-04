# tests/services/orchestrator/test_local_tools.py
from __future__ import annotations

import asyncio

import pytest

from services.orchestrator import events
from services.orchestrator.inproc_bus import EventBus
from services.orchestrator.local_tools import (
    LOCAL_TOOL_NAMES,
    TOOL_RESULTS_TOPIC_PREFIX,
    request_local_tool,
    write_tool_result,
)


def test_local_tool_names_are_the_three_file_tools():
    assert LOCAL_TOOL_NAMES == {"read_file", "write_file", "list_dir"}


async def test_request_local_tool_emits_event_and_returns_result():
    task_id = "task-abc"
    bus = EventBus()
    sub = bus.subscribe(f"{events.EVENTS_TOPIC_PREFIX}{task_id}")
    emitter = events.EventEmitter(bus, task_id)
    token = events.current_emitter.set(emitter)
    seen_reqs: list[dict] = []
    try:
        # Simulate the local client: once a tool.request is published on the
        # event bus, write a matching tool.result onto the tool-results topic.
        async def fake_client() -> None:
            async for ev in sub:
                seen_reqs.append(ev)
                if ev.get("type") == "tool.request":
                    await write_tool_result(
                        bus, task_id, ev["tool_request_id"], {"content": "hello"}
                    )
                    return

        client_task = asyncio.create_task(fake_client())
        out = await request_local_tool("read_file", {"path": "notes.txt"}, timeout=5.0)
        await client_task
        assert out == {"content": "hello"}

        # The tool.request event was published with the expected shape.
        reqs = [ev for ev in seen_reqs if ev.get("type") == "tool.request"]
        assert len(reqs) == 1
        assert reqs[0]["name"] == "read_file"
        assert reqs[0]["args"] == {"path": "notes.txt"}
        assert reqs[0]["task_id"] == task_id
        assert "tool_request_id" in reqs[0]
    finally:
        events.current_emitter.reset(token)
        sub.close()


async def test_request_local_tool_times_out_when_no_result():
    task_id = "task-timeout"
    bus = EventBus()
    emitter = events.EventEmitter(bus, task_id)
    token = events.current_emitter.set(emitter)
    try:
        with pytest.raises(TimeoutError):
            await request_local_tool("read_file", {"path": "x"}, timeout=0.3)
    finally:
        events.current_emitter.reset(token)


async def test_request_local_tool_matches_only_its_own_request_id():
    task_id = "task-mux"
    bus = EventBus()
    sub = bus.subscribe(f"{events.EVENTS_TOPIC_PREFIX}{task_id}")
    emitter = events.EventEmitter(bus, task_id)
    token = events.current_emitter.set(emitter)
    try:
        # The fake client only reacts once it observes the tool.request event,
        # which request_local_tool only emits AFTER it has subscribed to the
        # results topic — so both the stale (mismatched id) and real frames
        # below are published to an already-subscribed listener. The stale
        # frame must be skipped in favor of the one with the matching id.
        async def fake_client() -> None:
            async for ev in sub:
                if ev.get("type") == "tool.request":
                    # Stale/mismatched result first, then the real one.
                    await write_tool_result(bus, task_id, "other", 1)
                    await write_tool_result(bus, task_id, ev["tool_request_id"], 2)
                    return

        client_task = asyncio.create_task(fake_client())
        out = await request_local_tool("list_dir", {"path": "."}, timeout=5.0)
        await client_task
        assert out == 2
    finally:
        events.current_emitter.reset(token)
        sub.close()


async def test_request_local_tool_raises_on_error_frame():
    task_id = "task-err"
    bus = EventBus()
    sub = bus.subscribe(f"{events.EVENTS_TOPIC_PREFIX}{task_id}")
    emitter = events.EventEmitter(bus, task_id)
    token = events.current_emitter.set(emitter)
    try:

        async def fake_client_with_error() -> None:
            async for ev in sub:
                if ev.get("type") == "tool.request":
                    await write_tool_result(
                        bus, task_id, ev["tool_request_id"], None, error="permission denied"
                    )
                    return

        client_task = asyncio.create_task(fake_client_with_error())
        with pytest.raises(RuntimeError, match="permission denied"):
            await request_local_tool("read_file", {"path": "x"}, timeout=5.0)
        await client_task
    finally:
        events.current_emitter.reset(token)
        sub.close()


async def test_request_local_tool_raises_when_no_active_emitter():
    # No current_emitter set (and thus no bus reachable) -> a clear error,
    # not a hang.
    with pytest.raises(RuntimeError):
        await request_local_tool("read_file", {"path": "x"}, timeout=0.2)


async def test_write_tool_result_publishes_frame_to_results_topic():
    bus = EventBus()
    task_id = "task-w1"
    sub = bus.subscribe(f"{TOOL_RESULTS_TOPIC_PREFIX}{task_id}")
    await write_tool_result(bus, task_id, "req-42", {"ok": True})
    frame = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
    assert frame == {"tool_request_id": "req-42", "result": {"ok": True}, "error": None}
    sub.close()


async def test_write_tool_result_with_error():
    bus = EventBus()
    task_id = "task-w2"
    sub = bus.subscribe(f"{TOOL_RESULTS_TOPIC_PREFIX}{task_id}")
    await write_tool_result(bus, task_id, "req-99", None, error="path escape")
    frame = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
    assert frame["error"] == "path escape"
    assert frame["result"] is None
    sub.close()


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
from services.orchestrator.local_tools import (  # noqa: E402
    RUN_TESTS_TIMEOUT_MS_MAX,
    build_run_tests_command,
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
from services.orchestrator.local_tools import (  # noqa: E402
    SANDBOX_TEST_TIMEOUT_S_MAX,
    build_sandbox_test_args,
    shape_sandbox_test_result,
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
            "content": [
                {
                    "type": "text",
                    "text": '{"passed": 3, "failed": 0, "errors": 0, "output": "3 passed", "timed_out": false}',
                }
            ],
            "isError": False,
        },
    }
    out = shape_sandbox_test_result(envelope)
    assert out == {"ok": True, "exit_code": 0, "raw_output": "3 passed"}


def test_shape_sandbox_test_result_failing():
    envelope = {
        "ok": True,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": '{"passed": 1, "failed": 2, "errors": 0, "output": "FAILED test_x", "timed_out": false}',
                }
            ],
            "isError": True,
        },
    }
    out = shape_sandbox_test_result(envelope)
    assert out["ok"] is False
    assert out["exit_code"] == 1
    assert "FAILED" in out["raw_output"]


def test_shape_sandbox_test_result_infra_error():
    out = shape_sandbox_test_result(
        {"ok": False, "error": "skill_unavailable", "detail": "no tool"}
    )
    assert out["ok"] is False
    assert out["exit_code"] == 1
    assert "skill_unavailable" in out["raw_output"]


def test_shape_sandbox_test_result_timed_out():
    envelope = {
        "ok": True,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": '{"passed": 0, "failed": 0, "errors": 0, "output": "", "timed_out": true}',
                }
            ]
        },
    }
    out = shape_sandbox_test_result(envelope)
    assert out["ok"] is False
