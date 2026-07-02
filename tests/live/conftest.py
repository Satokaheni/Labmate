"""Gating for LIVE real-seam smoke tests.

These exercise actual execution seams (exec_run, code-sandbox, SkillRegistry)
and are SKIPPED unless LIVE_TESTS=1. Run on the deployment host before an A/B:
    LIVE_TESTS=1 python -m pytest tests/live -v
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Per-test timeout — live-suite only
# ---------------------------------------------------------------------------
# MCP subprocess teardown can deadlock: stdio_client.__aexit__ awaits the child
# exit, and if the child ignores cancellation the asyncio task never finishes.
# pytest-asyncio's _cancel_all_tasks then blocks on selector.select() forever.
#
# We apply a 120-second wall-clock timeout to every test collected under
# tests/live/.  method="thread" is required because teardown hangs occur after
# the coroutine has already returned, so SIGALRM (the default) can't interrupt
# them — only a daemon thread that raises SystemExit into the test thread can.
#
# SCOPE: only items whose nodeid path contains "tests/live" are affected.
# The unit/orchestrator/eval suites under tests/services and tests/eval are
# untouched by this hook — no per-test timeout is added there.
_LIVE_SUITE_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items):
    """Stamp every test in tests/live/ with a 120-second thread-method timeout.

    This is a guard against teardown deadlocks (orphaned MCP subprocesses whose
    stdio_client blocks the event loop); it does NOT affect the unit suite.
    """
    try:
        import pytest_timeout  # noqa: F401 — confirm plugin is installed
    except ImportError:  # pragma: no cover — missing dep is a soft error
        return

    timeout_marker = pytest.mark.timeout(120, method="thread")
    for item in items:
        try:
            item_path = Path(item.fspath).resolve()
        except Exception:  # noqa: BLE001
            continue
        if _LIVE_SUITE_DIR in item_path.parents or item_path.parent == _LIVE_SUITE_DIR:
            item.add_marker(timeout_marker, append=False)


def live_enabled() -> bool:
    return os.getenv("LIVE_TESTS") == "1"


def require_live() -> None:
    if not live_enabled():
        pytest.skip("LIVE_TESTS!=1 (set LIVE_TESTS=1 to run real-seam smoke tests)")


def require_service(check: Callable[[], bool], name: str) -> None:
    """Skip (not fail) when a needed live service is unreachable."""
    try:
        ok = check()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"live service {name!r} unreachable: {exc}")
    if not ok:
        pytest.skip(f"live service {name!r} not ready")


@pytest.fixture(autouse=True)
def _live_gate():
    require_live()
