"""Unit tests for the local-only state path helpers."""

from __future__ import annotations

from pathlib import Path

from services.orchestrator.local_mode import (
    local_state_db_path,
    local_state_dir,
)


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
