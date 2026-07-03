"""Unit tests for the LABMATE_LOCAL_MODE flag reader (Piece 0 seam)."""

from __future__ import annotations

import pytest

from services.orchestrator.local_mode import local_mode_enabled


def test_default_is_off(monkeypatch):
    """Unset env -> local mode OFF (pod mode is the default)."""
    monkeypatch.delenv("LABMATE_LOCAL_MODE", raising=False)
    assert local_mode_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "  1  ", "Local"])
def test_truthy_values_enable(monkeypatch, value):
    monkeypatch.setenv("LABMATE_LOCAL_MODE", value)
    assert local_mode_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "off", "", "  ", "  off  "])
def test_falsey_values_disable(monkeypatch, value):
    monkeypatch.setenv("LABMATE_LOCAL_MODE", value)
    assert local_mode_enabled() is False


def test_read_at_call_time_not_import(monkeypatch):
    """Flipping the env between calls is observed immediately (no import-time cache)."""
    monkeypatch.setenv("LABMATE_LOCAL_MODE", "0")
    assert local_mode_enabled() is False
    monkeypatch.setenv("LABMATE_LOCAL_MODE", "1")
    assert local_mode_enabled() is True
