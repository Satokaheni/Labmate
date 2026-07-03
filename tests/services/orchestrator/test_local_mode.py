"""Unit tests for the LABMATE_LOCAL_MODE flag reader (Piece 0 seam)."""

from __future__ import annotations

from pathlib import Path

import pytest

from services.orchestrator.local_mode import (
    local_mode_enabled,
    local_state_db_path,
    local_state_dir,
)


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


def test_main_imports_flag_reader():
    """main.py wires the flag reader (guards against the import being dropped)."""
    import services.orchestrator.main as main_mod

    assert hasattr(main_mod, "local_mode_enabled")
    # Callable and returns a bool regardless of env.
    assert isinstance(main_mod.local_mode_enabled(), bool)


def test_local_state_dir_default(monkeypatch):
    monkeypatch.delenv("LABMATE_STATE_DIR", raising=False)
    assert local_state_dir() == Path(".data")


def test_local_state_dir_env_override(monkeypatch):
    monkeypatch.setenv("LABMATE_STATE_DIR", "/tmp/lm-state")
    assert local_state_dir() == Path("/tmp/lm-state")


def test_local_state_db_path_default(monkeypatch):
    monkeypatch.delenv("LABMATE_STATE_DB", raising=False)
    monkeypatch.delenv("LABMATE_STATE_DIR", raising=False)
    assert local_state_db_path() == Path(".data") / "labmate_state.sqlite"


def test_local_state_db_path_follows_state_dir(monkeypatch):
    monkeypatch.delenv("LABMATE_STATE_DB", raising=False)
    monkeypatch.setenv("LABMATE_STATE_DIR", "/tmp/lm-state")
    assert local_state_db_path() == Path("/tmp/lm-state") / "labmate_state.sqlite"


def test_local_state_db_path_full_override(monkeypatch):
    monkeypatch.setenv("LABMATE_STATE_DB", "/tmp/custom/my.sqlite")
    monkeypatch.setenv("LABMATE_STATE_DIR", "/tmp/lm-state")  # ignored when DB override set
    assert local_state_db_path() == Path("/tmp/custom/my.sqlite")


def test_local_state_db_path_empty_override_falls_back(monkeypatch):
    monkeypatch.setenv("LABMATE_STATE_DB", "")  # empty = unset
    monkeypatch.delenv("LABMATE_STATE_DIR", raising=False)
    assert local_state_db_path() == Path(".data") / "labmate_state.sqlite"
