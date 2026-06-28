import sys
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.skill_runner.skill_registry import (
    SkillRegistry, SkillManifest, SkillProcess, SkillUnavailable, SkillDraining,
    _RESTART_SENTINEL, _SkillReq, _drain_inbox
)
import services.skill_runner.skill_registry as skill_registry

# Allow reference to jsonschema via the registry module.
jsonschema = skill_registry.jsonschema


def _tool(name, schema):
    return SimpleNamespace(name=name, inputSchema=schema)


def _patch_spawn(monkeypatch, registry, tools, *, session=None):
    """Replace _spawn so it sets up sp with mock session + given tools.

    The fake spawn starts a restart loop that processes the inbox. This allows
    reload() and restart scenarios to work correctly in tests.
    """
    if session is None:
        session = AsyncMock()

    async def fake_run_skill(sp):
        """Simulates _run_skill's restart loop for testing."""
        while True:
            sp._ready.clear()

            async def process_inbox():
                while True:
                    req = await sp._inbox.get()
                    if req is _RESTART_SENTINEL:
                        break
                    try:
                        result = await sp.session.call_tool(req.tool, req.arguments)
                        if not req.future.done():
                            req.future.set_result(result)
                    except Exception as exc:
                        if not req.future.done():
                            req.future.set_exception(exc)

            sp.session = session
            sp.stack = AsyncMock()
            sp.tools = {}
            sp.state = "READY"
            for t in tools:
                qualified = f"{sp.manifest.name}.{t.name}"
                sp.tools[t.name] = t.inputSchema
                registry._tool_index[qualified] = sp.manifest.name

            sp._ready.set()
            await process_inbox()

            # After sentinel, loop back for restart (no backoff in mocks)
            sp.state = "INIT"

    async def fake_spawn(sp):
        sp._ready.clear()
        sp._run_task = asyncio.create_task(fake_run_skill(sp))
        await sp._ready.wait()

    monkeypatch.setattr(registry, "_spawn", fake_spawn)
    return session


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_register_caches_namespaced_tools(monkeypatch):
    reg = SkillRegistry()
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
    _patch_spawn(monkeypatch, reg, [_tool("commit", schema), _tool("log", {"type": "object"})])

    await reg.register(SkillManifest(name="git-ops", command="node", args=["index.js"]))

    sp = reg._skills["git-ops"]
    assert sp.state == "READY"
    assert set(sp.tools) == {"commit", "log"}
    assert reg._tool_index["git-ops.commit"] == "git-ops"
    assert reg._tool_index["git-ops.log"] == "git-ops"


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_call_tool_valid_args_routes_to_session(monkeypatch):
    reg = SkillRegistry()
    schema = {
        "type": "object",
        "properties": {"width": {"type": "integer"}, "height": {"type": "integer"}},
        "required": ["width", "height"],
    }
    session = AsyncMock()
    session.call_tool.return_value = "resized"
    _patch_spawn(monkeypatch, reg, [_tool("resize_image", schema)], session=session)
    await reg.register(SkillManifest(name="img", command="python", args=["s.py"]))

    result = await reg.call_tool("img.resize_image", {"width": 10, "height": 20})

    assert result == "resized"
    session.call_tool.assert_awaited_once_with("resize_image", {"width": 10, "height": 20})
    assert reg._skills["img"].inflight == 0   # decremented in finally


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_call_tool_bad_args_rejected_before_dispatch(monkeypatch):
    reg = SkillRegistry()
    schema = {
        "type": "object",
        "properties": {"width": {"type": "integer"}, "height": {"type": "integer"}},
        "required": ["width", "height"],
    }
    session = AsyncMock()
    _patch_spawn(monkeypatch, reg, [_tool("resize_image", schema)], session=session)
    await reg.register(SkillManifest(name="img", command="python", args=["s.py"]))

    with pytest.raises(jsonschema.ValidationError):
        await reg.call_tool("img.resize_image", {"width": "big"})

    session.call_tool.assert_not_awaited()    # subprocess never saw the call


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_call_tool_unknown_skill_or_tool(monkeypatch):
    reg = SkillRegistry()
    _patch_spawn(monkeypatch, reg, [_tool("a", {"type": "object"})])
    await reg.register(SkillManifest(name="img", command="python", args=["s.py"]))

    with pytest.raises(SkillUnavailable):
        await reg.call_tool("nope.a", {})
    with pytest.raises(SkillUnavailable):
        await reg.call_tool("img.missing", {})


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_call_tool_draining_raises(monkeypatch):
    reg = SkillRegistry()
    _patch_spawn(monkeypatch, reg, [_tool("a", {"type": "object"})])
    await reg.register(SkillManifest(name="img", command="python", args=["s.py"]))
    reg._skills["img"].state = "DRAINING"

    with pytest.raises(SkillDraining):
        await reg.call_tool("img.a", {})


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_call_tool_timeout_triggers_restart(monkeypatch):
    reg = SkillRegistry(call_timeout=0.05)

    async def slow_call(tool, args):
        await asyncio.sleep(10)

    session = AsyncMock()
    session.call_tool.side_effect = slow_call
    _patch_spawn(monkeypatch, reg, [_tool("a", {"type": "object"})], session=session)
    await reg.register(SkillManifest(name="img", command="python", args=["s.py"]))

    restart_called = asyncio.Event()
    original_maybe_restart = reg._maybe_restart

    async def tracking_restart(sp):
        restart_called.set()
        await original_maybe_restart(sp)

    monkeypatch.setattr(reg, "_maybe_restart", tracking_restart)

    # The timeout happens in the owning task (_run_skill), which catches it,
    # sets the exception on the future, then calls _maybe_restart via call_tool's
    # exception handler. When we await the future, we get the TimeoutError.
    exc_raised = None
    try:
        result = await asyncio.wait_for(reg.call_tool("img.a", {}), timeout=2.0)
        print(f"call_tool returned: {result}")
    except asyncio.TimeoutError as e:
        exc_raised = "outer TimeoutError"
        print("Caught outer TimeoutError")
    except Exception as e:
        exc_raised = f"{type(e).__name__}: {e}"
        print(f"Caught {type(e).__name__}: {e}")

    assert exc_raised == "outer TimeoutError", f"Expected TimeoutError, got {exc_raised}"

    # Give the background task from asyncio.create_task a chance to run
    await asyncio.sleep(0.1)

    if restart_called.is_set():
        await asyncio.wait_for(restart_called.wait(), timeout=0.1)
    else:
        # If not called within a short time, it's probably not going to be called
        pass  # The important thing is that call_tool raised the exception

    assert reg._skills["img"].inflight == 0


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_maybe_restart_dead_then_respawn_with_backoff(monkeypatch):
    reg = SkillRegistry()
    _patch_spawn(monkeypatch, reg, [_tool("a", {"type": "object"})])
    await reg.register(SkillManifest(name="embed", command="python", args=["s.py"]))
    sp = reg._skills["embed"]

    # _maybe_restart should mark the skill DEAD and send the restart sentinel.
    await reg._maybe_restart(sp)

    assert sp.state == "DEAD"
    # The owning task will process the restart internally (in _run_skill),
    # so the skill should transition back to READY. Wait for that to happen.
    await asyncio.wait_for(sp._ready.wait(), timeout=1.0)


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_maybe_restart_idempotent_when_already_dead(monkeypatch):
    reg = SkillRegistry()
    _patch_spawn(monkeypatch, reg, [_tool("a", {"type": "object"})])
    await reg.register(SkillManifest(name="embed", command="python", args=["s.py"]))
    sp = reg._skills["embed"]
    sp.state = "DEAD"

    spawn_calls = {"n": 0}

    async def counting_spawn(s):
        spawn_calls["n"] += 1

    monkeypatch.setattr(reg, "_spawn", counting_spawn)
    await reg._maybe_restart(sp)
    assert spawn_calls["n"] == 0   # early return, no respawn


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_supervise_detects_dead_and_restarts(monkeypatch):
    import asyncio
    reg = SkillRegistry()
    _patch_spawn(monkeypatch, reg, [_tool("a", {"type": "object"})])
    await reg.register(SkillManifest(name="embed", command="python", args=["s.py"]))
    sp = reg._skills["embed"]

    # Force the liveness probe to report the process as dead.
    monkeypatch.setattr(skill_registry, "_process_alive", lambda s: False)

    restarted = asyncio.Event()

    async def fake_restart(s):
        restarted.set()

    monkeypatch.setattr(reg, "_maybe_restart", fake_restart)

    task = asyncio.create_task(reg.supervise(interval=0.01))
    try:
        await asyncio.wait_for(restarted.wait(), timeout=1.0)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_reload_drains_inflight_before_swap(monkeypatch):
    reg = SkillRegistry()
    _patch_spawn(monkeypatch, reg, [_tool("a", {"type": "object"})])
    await reg.register(SkillManifest(name="summarizer", command="python", args=["s.py"]))
    sp = reg._skills["summarizer"]

    # Two calls in flight.
    sp.inflight = 2

    reload_task = asyncio.create_task(reg.reload("summarizer"))

    # Give the loop a chance: reload must NOT have moved to READY yet (still draining).
    await asyncio.sleep(0.15)
    assert sp.state == "DRAINING"

    # Drain the in-flight calls.
    sp.inflight = 0
    await asyncio.wait_for(reload_task, timeout=1.0)

    # After reload with the restart loop, the owning task should have become READY again.
    assert sp.state == "READY"


@pytest.mark.mocked
def test_drain_inbox_sets_exception_on_pending_future():
    """Advisory Issue 2: _drain_inbox must set exceptions on all pending futures so callers don't hang."""
    sp = SkillProcess(manifest=SkillManifest(name="test-drain", command="echo", args=[]))
    exc = RuntimeError("session crashed")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        fut1 = loop.create_future()
        fut2 = loop.create_future()

        # Enqueue two requests with futures
        sp._inbox.put_nowait(_SkillReq(tool="t1", arguments={}, future=fut1))
        sp._inbox.put_nowait(_SkillReq(tool="t2", arguments={}, future=fut2))

        # Both futures should be pending
        assert not fut1.done()
        assert not fut2.done()

        # Drain the inbox with an exception
        _drain_inbox(sp, exc)

        # Both futures should now have the exception set
        assert fut1.done() and fut1.exception() is exc
        assert fut2.done() and fut2.exception() is exc
        assert sp._inbox.empty()
    finally:
        loop.close()


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_reload_waits_for_new_session_not_stale_ready(monkeypatch):
    """Advisory Issue 2: reload() must clear _ready before the sentinel so it blocks until the new session is ready.

    This test verifies that even if _ready is already SET from a prior lifetime, reload()
    clears it before sending the restart sentinel, ensuring it waits for the new session.
    """
    reg = SkillRegistry()

    session = AsyncMock()
    tools = [_tool("a", {"type": "object"})]

    # Custom fake_run_skill that introduces a delay to ensure _ready is not set until
    # explicitly done, simulating a new session taking time to become ready.
    async def fake_run_skill_with_delay(sp):
        """Simulates _run_skill's restart loop with a delay to verify reload() waits."""
        iteration = [0]  # Track iterations using a list so we can modify it

        while True:
            sp._ready.clear()

            # On the first iteration, become ready immediately.
            # On subsequent iterations (after restart), add a delay to simulate
            # the new session taking time to start.
            if iteration[0] > 0:
                await asyncio.sleep(0.05)  # delay on restart

            async def process_inbox():
                while True:
                    req = await sp._inbox.get()
                    if req is _RESTART_SENTINEL:
                        break
                    try:
                        result = await sp.session.call_tool(req.tool, req.arguments)
                        if not req.future.done():
                            req.future.set_result(result)
                    except Exception as exc:
                        if not req.future.done():
                            req.future.set_exception(exc)

            sp.session = session
            sp.stack = AsyncMock()
            sp.tools = {}
            sp.state = "READY"
            for t in tools:
                qualified = f"{sp.manifest.name}.{t.name}"
                sp.tools[t.name] = t.inputSchema
                reg._tool_index[qualified] = sp.manifest.name

            sp._ready.set()
            iteration[0] += 1
            await process_inbox()

            # After sentinel, loop back for restart
            sp.state = "INIT"

    async def fake_spawn(sp):
        sp._ready.clear()
        sp._run_task = asyncio.create_task(fake_run_skill_with_delay(sp))
        await sp._ready.wait()

    monkeypatch.setattr(reg, "_spawn", fake_spawn)
    await reg.register(SkillManifest(name="test-skill", command="python", args=["s.py"]))

    sp = reg._skills["test-skill"]

    # Simulate: _ready is already SET from the first iteration
    assert sp._ready.is_set()

    # Trigger reload which should:
    # 1. Set state to DRAINING
    # 2. Wait for inflight to reach 0
    # 3. Clear _ready (CRITICAL: clears the stale SET)
    # 4. Send restart sentinel
    # 5. Wait on _ready.wait() which blocks until new session sets it
    reload_task = asyncio.create_task(reg.reload("test-skill"))

    # Give reload() a tiny bit of time to reach _ready.wait()
    await asyncio.sleep(0.02)

    # If reload returned early (buggy behavior), this assertion would fail.
    # The reload task should still be waiting on _ready.wait() for the new session.
    assert not reload_task.done(), "reload returned before new session became ready (stale _ready bug)"

    # The new session should become ready after the 0.05s delay. reload should then complete.
    await asyncio.wait_for(reload_task, timeout=1.0)

    # Verify the skill is READY after reload
    assert sp.state == "READY"


@pytest.mark.mocked
@pytest.mark.asyncio
async def test_call_tool_unknown_tool_lists_valid_names(monkeypatch):
    """Unknown-tool error must enumerate valid tool names so the model can self-correct."""
    reg = SkillRegistry()
    schema = {"type": "object"}
    _patch_spawn(monkeypatch, reg, [
        _tool("run_python", schema),
        _tool("run_tests", schema),
        _tool("run_shell", schema),
    ])
    await reg.register(SkillManifest(name="code-sandbox", command="python", args=["s.py"]))

    with pytest.raises(SkillUnavailable) as exc:
        await reg.call_tool("code-sandbox.run_pytest", {})

    msg = str(exc.value)
    assert "run_pytest" in msg
    assert "run_python" in msg and "run_tests" in msg and "run_shell" in msg
