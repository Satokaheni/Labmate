import pytest

from services.ws_gateway.boot import (
    boot_plan,
    check_brain,
    check_hands,
    check_memory,
    run_boot_sequence,
)


def test_boot_plan_lists_all_subsystems():
    plan = boot_plan()
    ids = [s["id"] for s in plan]
    assert ids == ["brain", "nervous_system", "hands", "memory", "workspace"]
    assert all(s["state"] == "pending" for s in plan)
    assert plan[0]["required"] is True  # brain required


@pytest.mark.asyncio
async def test_check_brain_ready_when_healthz_ok():
    async def fake_get(url):
        class R:
            status = 200

        return R()

    state, detail, message = await check_brain(http_get=fake_get, base_url="http://x:8000")
    assert state == "ready"


@pytest.mark.asyncio
async def test_check_brain_failed_when_healthz_errors():
    async def fake_get(url):
        raise ConnectionError("refused")

    state, detail, message = await check_brain(http_get=fake_get, base_url="http://x:8000")
    assert state == "failed"
    assert "refused" in message


@pytest.mark.asyncio
async def test_check_memory_ready_when_redis_pings(redis):
    state, detail, message = await check_memory(redis=redis)
    assert state == "ready"


@pytest.mark.asyncio
async def test_check_hands_counts_skill_dirs(tmp_path):
    (tmp_path / "web_search").mkdir()
    (tmp_path / "code_sandbox").mkdir()
    (tmp_path / "not_a_dir.txt").write_text("x")
    state, detail, message = await check_hands(skills_dir=str(tmp_path))
    assert state == "ready"
    assert "2" in detail


@pytest.mark.asyncio
async def test_run_boot_sequence_emits_updates_then_ready(redis):
    emitted = []

    async def emit(ev):
        emitted.append(ev)

    async def all_ready_check(**_):
        return ("ready", "ok", "")

    checks = {
        "brain": all_ready_check,
        "nervous_system": all_ready_check,
        "hands": all_ready_check,
        "memory": all_ready_check,
        "workspace": all_ready_check,
    }
    await run_boot_sequence(emit, checks)

    types = [e["type"] for e in emitted]
    assert types[0] == "boot.plan"
    assert "boot.update" in types
    assert types[-1] == "boot.ready"
    # one starting + one ready update per subsystem (5*2 = 10) between plan and ready
    updates = [e for e in emitted if e["type"] == "boot.update"]
    assert len(updates) == 10


@pytest.mark.asyncio
async def test_run_boot_sequence_warm_start_includes_sessions():
    """boot.ready must include sessions from the store if one is provided."""
    from services.ws_gateway.sessions import InMemorySessionStore

    store = InMemorySessionStore()
    await store.create(title="My session", mode="chat", session_id="s-abc")

    emitted = []

    async def emit(ev):
        emitted.append(ev)

    async def ready_check(**_):
        return ("ready", "ok", "")

    checks = {k: ready_check for k in ("brain", "nervous_system", "hands", "memory", "workspace")}
    await run_boot_sequence(emit, checks, session_store=store)

    boot_ready = next(e for e in emitted if e["type"] == "boot.ready")
    assert boot_ready["sessionBootstrap"]["sessions"] == await store.list()
    assert boot_ready["sessionBootstrap"]["activeSessionId"] == "s-abc"


@pytest.mark.asyncio
async def test_run_boot_sequence_empty_store_sends_empty_sessions():
    """boot.ready with no sessions → sessions=[], activeSessionId=None."""
    from services.ws_gateway.sessions import InMemorySessionStore

    emitted = []

    async def emit(ev):
        emitted.append(ev)

    async def ready_check(**_):
        return ("ready", "ok", "")

    checks = {k: ready_check for k in ("brain", "nervous_system", "hands", "memory", "workspace")}
    await run_boot_sequence(emit, checks, session_store=InMemorySessionStore())
    boot_ready = next(e for e in emitted if e["type"] == "boot.ready")
    assert boot_ready["sessionBootstrap"]["sessions"] == []
    assert boot_ready["sessionBootstrap"]["activeSessionId"] is None
