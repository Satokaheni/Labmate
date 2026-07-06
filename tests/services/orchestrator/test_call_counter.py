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


# ---------------------------------------------------------------------------
# note_prompt_tokens — feed a locally-measured prompt size into the gauge's
# high-water mark (for servers that omit usage.prompt_tokens, e.g. RunPod).
# ---------------------------------------------------------------------------


def test_note_prompt_tokens_sets_peak():
    token = call_counter.start()
    try:
        assert call_counter.get_peak_prompt_tokens() == 0
        call_counter.note_prompt_tokens(6000)
        assert call_counter.get_peak_prompt_tokens() == 6000
    finally:
        call_counter.reset(token)


def test_note_prompt_tokens_is_monotonic_high_water_mark():
    token = call_counter.start()
    try:
        call_counter.note_prompt_tokens(6000)
        call_counter.note_prompt_tokens(4000)  # smaller — peak must not drop
        assert call_counter.get_peak_prompt_tokens() == 6000
        call_counter.note_prompt_tokens(6100)  # larger — peak rises
        assert call_counter.get_peak_prompt_tokens() == 6100
    finally:
        call_counter.reset(token)


def test_note_prompt_tokens_noop_without_counter_or_nonpositive():
    assert call_counter.current_counter.get() is None
    call_counter.note_prompt_tokens(6000)  # no counter -> no raise, no-op
    assert call_counter.get_peak_prompt_tokens() == 0
    token = call_counter.start()
    try:
        call_counter.note_prompt_tokens(0)  # non-positive -> ignored
        call_counter.note_prompt_tokens(-5)
        assert call_counter.get_peak_prompt_tokens() == 0
    finally:
        call_counter.reset(token)
