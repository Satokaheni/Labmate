"""A/B routing — per-task llm_calls counter (call_counter.py).

The litellm success callback increments the current ContextVar counter; with no
counter set it is a no-op (never raises); two tokens are isolated. NEW file.
"""
from __future__ import annotations

from services.orchestrator import call_counter


def test_callback_increments_current_counter():
    token = call_counter.start()
    try:
        assert call_counter.get_count() == 0
        call_counter._increment_current()
        call_counter._increment_current()
        assert call_counter.get_count() == 2
    finally:
        call_counter.reset(token)


def test_callback_noop_without_counter():
    # No counter set in this context.
    assert call_counter.current_counter.get() is None
    # Must not raise.
    call_counter._increment_current()
    assert call_counter.get_count() == 0


def test_tokens_are_isolated():
    outer = call_counter.start()
    try:
        call_counter._increment_current()
        assert call_counter.get_count() == 1
        inner = call_counter.start()
        try:
            # Fresh counter for the inner scope.
            assert call_counter.get_count() == 0
            call_counter._increment_current()
            call_counter._increment_current()
            assert call_counter.get_count() == 2
        finally:
            call_counter.reset(inner)
        # Outer counter is restored, unaffected by the inner scope.
        assert call_counter.get_count() == 1
    finally:
        call_counter.reset(outer)


def test_registered_logger_increments_via_log_success_event():
    """The registered CustomLogger's success hook increments the current counter."""
    logger = call_counter._CALL_COUNT_LOGGER
    assert logger is not None  # registration succeeded at import
    token = call_counter.start()
    try:
        logger.log_success_event({}, None, 0.0, 0.0)
        assert call_counter.get_count() == 1
    finally:
        call_counter.reset(token)
