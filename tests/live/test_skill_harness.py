import pytest
from tests.live.skill_harness import declared_tools, runnable_manifests, result_text, result_is_error

pytestmark = pytest.mark.live


def test_declared_tools_parses_code_sandbox():
    tools = declared_tools("code-sandbox")
    assert {"run_python", "run_shell", "run_tests", "install_packages"} <= tools


def test_declared_tools_unknown_skill_is_empty():
    assert declared_tools("does-not-exist") == set()


def test_runnable_manifests_includes_code_sandbox():
    names = {m.name for m in runnable_manifests()}
    assert "code-sandbox" in names
    # academic-writing gained a server.py wrapper, so it is now runnable
    # (a skill is included iff it ships a server.py or dist/index.js).
    assert "academic-writing" in names


def test_dist_stale_detects_newer_src(tmp_path):
    import os
    from tests.live.skill_harness import _dist_stale

    dist = tmp_path / "dist" / "index.js"
    dist.parent.mkdir()
    dist.write_text("compiled")
    src = tmp_path / "src"
    src.mkdir()
    ts = src / "index.ts"
    ts.write_text("source")

    os.utime(dist, (1000, 1000))
    os.utime(ts, (2000, 2000))  # src newer than dist -> stale
    assert _dist_stale(dist, src) is True

    os.utime(dist, (3000, 3000))  # dist newer than src -> fresh
    assert _dist_stale(dist, src) is False


def test_dist_stale_missing_paths_not_stale(tmp_path):
    from tests.live.skill_harness import _dist_stale
    assert _dist_stale(tmp_path / "nope.js", tmp_path / "nosrc") is False


class _C:
    def __init__(self, text): self.text = text


class _R:
    def __init__(self, content, is_error=False):
        self.content = content
        self.isError = is_error


def test_result_text_joins_content():
    r = _R([_C("hello"), _C("world")])
    assert result_text(r) == "hello\nworld"


def test_result_is_error_reads_flag():
    assert result_is_error(_R([], is_error=True)) is True
    assert result_is_error(_R([_C("ok")])) is False


# --- per-skill timeout guards (the multi-hour `tests/live` hang fix) ---

@pytest.mark.asyncio
async def test_teardown_skill_bounded_when_task_ignores_cancel(monkeypatch):
    """A skill task that swallows CancelledError must not hang teardown."""
    import asyncio
    from tests.live import skill_harness

    monkeypatch.setattr(skill_harness, "TEARDOWN_TIMEOUT", 0.3)

    async def _wedged():
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                # deliberately refuse to die, like a wedged subprocess unwind
                continue

    class _SP:
        def __init__(self, task):
            self._run_task = task

    task = asyncio.ensure_future(_wedged())
    sp = _SP(task)
    loop = asyncio.get_event_loop()
    start = loop.time()
    await skill_harness.teardown_skill(reg=None, sp=sp)  # must return ~0.3s, not hang
    assert loop.time() - start < 2.0, "teardown_skill did not honor TEARDOWN_TIMEOUT"
    # orphaned task is left cancelled-but-pending; clean it up for the test
    task.cancel()


@pytest.mark.asyncio
async def test_teardown_skill_no_task_is_noop():
    from tests.live import skill_harness

    class _SP:
        _run_task = None

    await skill_harness.teardown_skill(reg=None, sp=_SP())  # returns immediately


@pytest.mark.asyncio
async def test_register_skill_raises_on_hung_registration(monkeypatch):
    """If registration never reaches READY, the hard ceiling raises (not hang)."""
    import asyncio
    from tests.live import skill_harness
    from tests.live.skill_harness import SkillRegisterError

    async def _never_returns(manifest, timeout):
        await asyncio.sleep(3600)

    monkeypatch.setattr(skill_harness, "_register_skill_inner", _never_returns)
    monkeypatch.setattr(skill_harness, "node_build_is_stale", lambda m: False)

    class _M:
        name = "fake-skill"

    with pytest.raises(SkillRegisterError, match="hard ceiling"):
        await skill_harness.register_skill(_M(), timeout=0.2)
