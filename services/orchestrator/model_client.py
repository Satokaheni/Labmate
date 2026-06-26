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
    raw = min(base_s * (2 ** attempt), max_s)
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
    attempts_cap = max_attempts_per_base if max_attempts_per_base is not None else _DEFAULT_MAX_ATTEMPTS
    bb = base_backoff_s if base_backoff_s is not None else _DEFAULT_BACKOFF_BASE
    mb = max_backoff_s if max_backoff_s is not None else _DEFAULT_BACKOFF_MAX

    history: list[tuple[str, Exception]] = []
    for base in bases:
        for attempt in range(attempts_cap):
            try:
                return await _acompletion(
                    model=model, api_base=base, api_key=api_key, **kwargs
                )
            except Exception as exc:  # noqa: BLE001 — classify, then re-raise or retry
                if not is_retryable(exc):
                    # 4xx / unknown: terminal, surface immediately, no failover.
                    raise
                history.append((base, exc))
                _log.warning(
                    "model endpoint %s attempt %d/%d failed (%s); will retry/failover",
                    base, attempt + 1, attempts_cap, type(exc).__name__,
                )
                # Sleep only if another attempt on THIS base remains.
                if attempt + 1 < attempts_cap:
                    await sleep(backoff_delay(attempt, bb, mb, rng))
    raise AllEndpointsExhausted(history)
