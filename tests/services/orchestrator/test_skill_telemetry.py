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


def _entry_last_used(days_ago: int, *, pinned: bool = False) -> dict:
    e = st.new_entry(T0 - timedelta(days=days_ago))
    e["last_used_at"] = (T0 - timedelta(days=days_ago)).isoformat()
    e["pinned"] = pinned
    return e


def test_compute_state_recent_is_active():
    e = _entry_last_used(5)
    assert st.compute_state(e, now=T0) == st.STATE_ACTIVE


def test_compute_state_stale_at_threshold():
    e = _entry_last_used(30)  # exactly stale_after_days
    assert st.compute_state(e, now=T0) == st.STATE_STALE


def test_compute_state_just_below_stale_is_active():
    e = _entry_last_used(29)
    assert st.compute_state(e, now=T0) == st.STATE_ACTIVE


def test_compute_state_archived_at_threshold():
    e = _entry_last_used(90)  # exactly archive_after_days
    assert st.compute_state(e, now=T0) == st.STATE_ARCHIVED


def test_compute_state_between_thresholds_is_stale():
    e = _entry_last_used(60)
    assert st.compute_state(e, now=T0) == st.STATE_STALE


def test_pinned_skill_never_transitions():
    e = _entry_last_used(365, pinned=True)
    assert st.compute_state(e, now=T0) == st.STATE_ACTIVE


def test_never_used_entry_measures_idle_from_created_at():
    e = st.new_entry(T0 - timedelta(days=100))  # last_used_at is None
    assert st.compute_state(e, now=T0) == st.STATE_ARCHIVED


def test_custom_thresholds_are_honored():
    e = _entry_last_used(10)
    assert st.compute_state(e, now=T0, stale_after_days=7, archive_after_days=20) == st.STATE_STALE


def test_apply_transitions_updates_all_entries():
    store = {"version": 1, "skills": {
        "fresh": _entry_last_used(1),
        "old": _entry_last_used(45),
        "ancient": _entry_last_used(120),
    }}
    out = st.apply_transitions(store, now=T0)
    assert out["skills"]["fresh"]["state"] == st.STATE_ACTIVE
    assert out["skills"]["old"]["state"] == st.STATE_STALE
    assert out["skills"]["ancient"]["state"] == st.STATE_ARCHIVED
    # input store untouched
    assert store["skills"]["old"]["state"] == st.STATE_ACTIVE
