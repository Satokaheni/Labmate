"""Resilient model client: ordered-endpoint failover over litellm.acompletion.

Single-GPU reality (CLAUDE.md): there is normally ONE model endpoint. This module
adds bounded retry on transient transport errors plus optional failover to extra
replica endpoints listed in LABMATE_FALLBACK_BASES. 4xx content errors never
trigger failover — they are surfaced immediately.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time

import litellm

_log = logging.getLogger("orchestrator.model_client")

# Transient transport errors → retry / fail over to the next endpoint.
_RETRYABLE = (
    litellm.APIConnectionError,
    litellm.Timeout,
    litellm.InternalServerError,
    litellm.ServiceUnavailableError,
    litellm.BadGatewayError,
    litellm.RateLimitError,
)

# 4xx content errors → terminal, never fail over.
_TERMINAL = (
    litellm.BadRequestError,
    litellm.AuthenticationError,
    litellm.NotFoundError,
    litellm.PermissionDeniedError,
    litellm.UnprocessableEntityError,
    litellm.ContextWindowExceededError,
)

_DEFAULT_MAX_ATTEMPTS = int(os.getenv("LABMATE_MODEL_MAX_ATTEMPTS_PER_BASE", "2"))
_DEFAULT_BACKOFF_BASE = float(os.getenv("LABMATE_MODEL_BACKOFF_BASE_S", "0.5"))
_DEFAULT_BACKOFF_MAX = float(os.getenv("LABMATE_MODEL_BACKOFF_MAX_S", "4.0"))

# ── thinking_budget_tokens self-heal (ml-intern G5, adapted) ─────────────────
# CLAUDE.md rule #6 sends extra_body={"thinking_budget_tokens": N} on every call.
# A served build/model that does NOT recognize the param answers 400 — and since
# a 4xx is terminal (no failover), EVERY call would then hard-fail with no
# recovery (an older llama.cpp build, a non-thinking model, or a differing user
# rig). ml-intern probes reasoning-effort per model and caches the working value;
# Labmate has one param and one endpoint kind, so the adaptation is a one-shot
# self-heal: on a 4xx that names the param/extra_body as unsupported, strip
# thinking_budget_tokens, retry once, and CACHE the base so later calls skip it.
#
# Stripping is safe precisely BECAUSE we only do it after a rejection: the
# INT_MAX-default hang rule #6 warns about exists only in builds that RECOGNIZE
# the param, and such a build would not have 400'd. Kill-switch: set
# LABMATE_THINKING_BUDGET_SELF_HEAL=0 to disable.
_THINKING_BUDGET_SELF_HEAL = os.getenv("LABMATE_THINKING_BUDGET_SELF_HEAL", "1") != "0"

# Bases learned to reject thinking_budget_tokens (process-wide, session-scoped —
# mirrors ml-intern caching the working effort per model on the session).
_NO_THINKING_BUDGET_BASES: set[str] = set()

# Lower-cased substrings in a 4xx body that specifically indicate the
# thinking_budget_tokens / extra_body field is unsupported — NOT a generic 400
# (a bare "bad request" must not trip the strip, or we'd mask real errors).
# "extra_body" is safe as a marker only because thinking_budget_tokens is the
# ONLY key Labmate puts in extra_body on these calls (CLAUDE.md rule #6); if that
# ever changes, gate it on a "thinking" check so a different extra_body param's
# rejection can't strip the budget.
_THINKING_BUDGET_ERROR_MARKERS = (
    "thinking_budget",
    "thinking budget",
    "extra_body",
    "unknown field",
    "unexpected keyword",
    "unrecognized request argument",
    "unsupported parameter",
    "additionalproperties",
    "additional properties",
)

# NEGATIVE guard: phrases that mean a VALUE was rejected, not the field itself. A
# build that HONORS thinking_budget_tokens but dislikes the value still NAMES the
# param in its 400 ("unsupported value for thinking_budget_tokens") — which would
# match the field markers above. Stripping there would drop a param the server
# honors → INT_MAX-default hang (rule #6). So if any value-error phrase is present
# we do NOT treat it as a field-unsupported rejection. Labmate only ever sends a
# fixed, valid budget, so this is belt-and-suspenders, but it removes the one hole
# in the "safe because we only strip after a rejection" argument.
_VALUE_ERROR_MARKERS = (
    "unsupported value",
    "invalid value",
    "value error",
    "must be",
    "out of range",
    "out-of-range",
)


def _has_thinking_budget(kwargs: dict) -> bool:
    """True if kwargs carry extra_body.thinking_budget_tokens."""
    eb = kwargs.get("extra_body")
    return isinstance(eb, dict) and "thinking_budget_tokens" in eb


def _strip_thinking_budget(kwargs: dict) -> dict:
    """Return a shallow copy of kwargs with extra_body.thinking_budget_tokens
    removed. No-op (returns the same object) when the param is absent."""
    eb = kwargs.get("extra_body")
    if not isinstance(eb, dict) or "thinking_budget_tokens" not in eb:
        return kwargs
    new = dict(kwargs)
    new_eb = dict(eb)
    new_eb.pop("thinking_budget_tokens", None)
    new["extra_body"] = new_eb
    return new


def _is_thinking_budget_rejection(exc: Exception) -> bool:
    """True for a terminal (non-retryable) 4xx whose message names the
    thinking_budget/extra_body field as unsupported."""
    if is_retryable(exc):
        return False  # transport / 5xx / 429 — not a param problem
    msg = str(getattr(exc, "message", "") or exc).lower()
    if any(v in msg for v in _VALUE_ERROR_MARKERS):
        return False  # a value complaint, not field-unsupported — do NOT strip
    return any(m in msg for m in _THINKING_BUDGET_ERROR_MARKERS)


def is_retryable(exc: Exception) -> bool:
    """True only for transient transport errors (conn-refused, timeout, 5xx, 429).

    4xx content errors and any unrecognized exception are NOT retryable — they are
    surfaced immediately with no failover.
    """
    if isinstance(exc, _TERMINAL):
        return False
    return isinstance(exc, _RETRYABLE)


def resolve_bases(primary: str, fallbacks_env: str | None = None) -> list[str]:
    """Build the ordered endpoint list: [primary, *fallbacks].

    fallbacks come from `fallbacks_env` (comma-separated). When None, reads
    LABMATE_FALLBACK_BASES. Blanks are dropped; exact duplicates are removed
    preserving first-seen order so the primary always leads.
    """
    if fallbacks_env is None:
        fallbacks_env = os.getenv("LABMATE_FALLBACK_BASES", "")
    raw = [primary, *(p.strip() for p in fallbacks_env.split(","))]
    out: list[str] = []
    for url in raw:
        if url and url not in out:
            out.append(url)
    return out


def backoff_delay(attempt: int, base_s: float, max_s: float, rng) -> float:
    """Exponential backoff with a [0.5, 1.0] jitter factor.

    attempt is 0-based. Raw delay = min(base_s * 2**attempt, max_s); the returned
    delay is that raw value scaled by (0.5 + 0.5*rng()), so jitter never drops the
    delay below half the curve. rng is a zero-arg callable returning a float in [0, 1).
    """
    raw = min(base_s * (2**attempt), max_s)
    jitter = 0.5 + 0.5 * rng()
    return raw * jitter


class AllEndpointsExhausted(RuntimeError):
    """All endpoints failed with retryable errors after bounded attempts."""

    def __init__(self, attempts: list[tuple[str, Exception]]) -> None:
        self.attempts = attempts
        bases = ", ".join(sorted({b for b, _ in attempts}))
        last = attempts[-1][1] if attempts else None
        super().__init__(
            f"all model endpoints exhausted after {len(attempts)} attempt(s) "
            f"across [{bases}]; last error: {type(last).__name__}: {last}"
        )


async def acompletion_with_failover(
    *,
    model: str,
    bases: list[str],
    api_key: str = "not-needed",
    max_attempts_per_base: int | None = None,
    base_backoff_s: float | None = None,
    max_backoff_s: float | None = None,
    sleep=asyncio.sleep,
    rng=random.random,
    _acompletion=None,
    **kwargs,
):
    """Call litellm.acompletion across an ordered endpoint list with failover.

    For each base in `bases`, retry up to `max_attempts_per_base` on retryable
    transport errors with jittered backoff between attempts. On a non-retryable
    (4xx) error, re-raise immediately (no failover). Returns the first success.
    Raises AllEndpointsExhausted when every base+attempt fails on retryable errors.

    `**kwargs` (messages, tools, tool_choice, stream, extra_body, ...) is forwarded
    verbatim — extra_body must already carry thinking_budget_tokens per CLAUDE.md.
    """
    if not bases:
        raise ValueError("acompletion_with_failover requires at least one base url")
    if _acompletion is None:
        _acompletion = litellm.acompletion
    attempts_cap = (
        max_attempts_per_base if max_attempts_per_base is not None else _DEFAULT_MAX_ATTEMPTS
    )
    bb = base_backoff_s if base_backoff_s is not None else _DEFAULT_BACKOFF_BASE
    mb = max_backoff_s if max_backoff_s is not None else _DEFAULT_BACKOFF_MAX

    history: list[tuple[str, Exception]] = []
    for base in bases:
        # Apply a previously-learned "this base rejects thinking_budget_tokens"
        # strip up front, so we never re-pay the rejection round-trip per turn.
        base_kwargs = (
            _strip_thinking_budget(kwargs)
            if _THINKING_BUDGET_SELF_HEAL and base in _NO_THINKING_BUDGET_BASES
            else kwargs
        )
        for attempt in range(attempts_cap):
            try:
                return await _acompletion(
                    model=model, api_base=base, api_key=api_key, **base_kwargs
                )
            except Exception as exc:  # noqa: BLE001 — classify, then re-raise or retry
                # One-shot self-heal (G5): the server rejected thinking_budget_tokens.
                # Cache the base, strip the param, and retry ONCE on this same base.
                if (
                    _THINKING_BUDGET_SELF_HEAL
                    and _has_thinking_budget(base_kwargs)
                    and _is_thinking_budget_rejection(exc)
                ):
                    _NO_THINKING_BUDGET_BASES.add(base)
                    base_kwargs = _strip_thinking_budget(base_kwargs)
                    _log.warning(
                        "base %s rejected thinking_budget_tokens (%s); retrying "
                        "without it and caching",
                        base,
                        type(exc).__name__,
                    )
                    try:
                        return await _acompletion(
                            model=model, api_base=base, api_key=api_key, **base_kwargs
                        )
                    except Exception as exc2:  # noqa: BLE001 — fall through to classify
                        exc = exc2
                if not is_retryable(exc):
                    # 4xx / unknown: terminal, surface immediately, no failover.
                    # `raise exc` (not bare `raise`) so a failed self-heal retry
                    # surfaces exc2 (the reassigned error), not the original.
                    raise exc
                history.append((base, exc))
                _log.warning(
                    "model endpoint %s attempt %d/%d failed (%s); will retry/failover",
                    base,
                    attempt + 1,
                    attempts_cap,
                    type(exc).__name__,
                )
                # Sleep only if another attempt on THIS base remains.
                if attempt + 1 < attempts_cap:
                    await sleep(backoff_delay(attempt, bb, mb, rng))
    raise AllEndpointsExhausted(history)


def completion_with_failover(
    *,
    model: str,
    bases: list[str],
    api_key: str = "not-needed",
    max_attempts_per_base: int | None = None,
    base_backoff_s: float | None = None,
    max_backoff_s: float | None = None,
    sleep=time.sleep,
    rng=random.random,
    _completion=None,
    **kwargs,
):
    """SYNC twin of acompletion_with_failover (over litellm.completion).

    For callers on a synchronous interface — notably SKILLS that use instructor's
    sync client (``instructor.from_litellm(resilient_completion)``). Same ordered-
    endpoint failover, bounded per-base retry with jittered backoff, and the G5
    thinking_budget self-heal. KEEP IN SYNC with acompletion_with_failover above.
    """
    if not bases:
        raise ValueError("completion_with_failover requires at least one base url")
    if _completion is None:
        _completion = litellm.completion
    attempts_cap = (
        max_attempts_per_base if max_attempts_per_base is not None else _DEFAULT_MAX_ATTEMPTS
    )
    bb = base_backoff_s if base_backoff_s is not None else _DEFAULT_BACKOFF_BASE
    mb = max_backoff_s if max_backoff_s is not None else _DEFAULT_BACKOFF_MAX

    history: list[tuple[str, Exception]] = []
    for base in bases:
        base_kwargs = (
            _strip_thinking_budget(kwargs)
            if _THINKING_BUDGET_SELF_HEAL and base in _NO_THINKING_BUDGET_BASES
            else kwargs
        )
        for attempt in range(attempts_cap):
            try:
                return _completion(model=model, api_base=base, api_key=api_key, **base_kwargs)
            except Exception as exc:  # noqa: BLE001 — classify, then re-raise or retry
                if (
                    _THINKING_BUDGET_SELF_HEAL
                    and _has_thinking_budget(base_kwargs)
                    and _is_thinking_budget_rejection(exc)
                ):
                    _NO_THINKING_BUDGET_BASES.add(base)
                    base_kwargs = _strip_thinking_budget(base_kwargs)
                    _log.warning(
                        "base %s rejected thinking_budget_tokens (%s); retrying "
                        "without it and caching",
                        base,
                        type(exc).__name__,
                    )
                    try:
                        return _completion(
                            model=model, api_base=base, api_key=api_key, **base_kwargs
                        )
                    except Exception as exc2:  # noqa: BLE001 — fall through to classify
                        exc = exc2
                if not is_retryable(exc):
                    raise exc
                history.append((base, exc))
                _log.warning(
                    "model endpoint %s attempt %d/%d failed (%s); will retry/failover",
                    base,
                    attempt + 1,
                    attempts_cap,
                    type(exc).__name__,
                )
                if attempt + 1 < attempts_cap:
                    sleep(backoff_delay(attempt, bb, mb, rng))
    raise AllEndpointsExhausted(history)


def resilient_completion(*, model: str, api_base: str | None = None, **kwargs):
    """``litellm.completion``-compatible callable with cross-endpoint failover +
    the shared retry / thinking_budget self-heal policy — the SHARED resilient path
    for SKILLS (and anything on the sync interface).

    Drop-in for ``instructor.from_litellm(resilient_completion)``: it resolves the
    ordered base list from ``api_base`` (or ``GEMMA_BASE``) + ``LABMATE_FALLBACK_BASES``
    and dispatches through completion_with_failover, so a skill's model call gets the
    same resilience as the orchestrator's instead of a naked ``litellm.completion``
    that dies on the first transient blip.
    """
    primary = api_base or os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
    return completion_with_failover(model=model, bases=resolve_bases(primary), **kwargs)
