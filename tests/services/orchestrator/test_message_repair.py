from __future__ import annotations

import pytest

from services.orchestrator.message_repair import (
    sanitize_messages,
    validate_messages,
    message_repair_enabled,
)


def test_enabled_defaults_on(monkeypatch):
    monkeypatch.delenv("ENABLE_MESSAGE_REPAIR", raising=False)
    assert message_repair_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "FALSE", " Off "])
def test_enabled_falsey_values_disable(monkeypatch, val):
    monkeypatch.setenv("ENABLE_MESSAGE_REPAIR", val)
    assert message_repair_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "ON"])
def test_enabled_truthy_values_enable(monkeypatch, val):
    monkeypatch.setenv("ENABLE_MESSAGE_REPAIR", val)
    assert message_repair_enabled() is True


def test_sanitize_returns_new_list_not_same_object():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]
    out = sanitize_messages(msgs)
    assert out is not msgs
    assert out == msgs


def test_validate_returns_list():
    assert validate_messages([{"role": "user", "content": "hi"}]) == []
