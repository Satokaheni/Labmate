"""Unit tests for the pure skill-telemetry store (no I/O unless noted)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.orchestrator import skill_telemetry as st

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_new_entry_defaults():
    e = st.new_entry(T0)
    assert e["use_count"] == 0
    assert e["success_count"] == 0
    assert e["fail_count"] == 0
    assert e["last_used_at"] is None
    assert e["created_by"] == "human"
    assert e["state"] == st.STATE_ACTIVE
    assert e["pinned"] is False
    assert e["created_at"] == T0.isoformat()


def test_record_use_success_bumps_use_and_success():
    store = {"version": 1, "skills": {}}
    out = st.record_use(store, "web-search", ok=True, now=T0)
    entry = out["skills"]["web-search"]
    assert entry["use_count"] == 1
    assert entry["success_count"] == 1
    assert entry["fail_count"] == 0
    assert entry["last_used_at"] == T0.isoformat()


def test_record_use_failure_bumps_fail_only():
    store = {"version": 1, "skills": {}}
    out = st.record_use(store, "web-search", ok=False, now=T0)
    entry = out["skills"]["web-search"]
    assert entry["use_count"] == 1
    assert entry["success_count"] == 0
    assert entry["fail_count"] == 1
    assert entry["last_used_at"] == T0.isoformat()


def test_record_use_accumulates_across_calls():
    store = {"version": 1, "skills": {}}
    store = st.record_use(store, "web-search", ok=True, now=T0)
    later = T0 + timedelta(days=1)
    store = st.record_use(store, "web-search", ok=False, now=later)
    entry = store["skills"]["web-search"]
    assert entry["use_count"] == 2
    assert entry["success_count"] == 1
    assert entry["fail_count"] == 1
    assert entry["last_used_at"] == later.isoformat()


def test_record_use_does_not_mutate_input_store():
    store = {"version": 1, "skills": {}}
    st.record_use(store, "web-search", ok=True, now=T0)
    assert store["skills"] == {}  # original untouched
