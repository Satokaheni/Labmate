"""Gating for LIVE real-seam smoke tests.

These exercise actual execution seams (exec_run, code-sandbox, SkillRegistry)
and are SKIPPED unless LIVE_TESTS=1. Run on the deployment host before an A/B:
    LIVE_TESTS=1 python -m pytest tests/live -v
"""
from __future__ import annotations

import os
from typing import Callable

import pytest


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
