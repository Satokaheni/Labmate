"""Resilient model client: ordered-endpoint failover over litellm.acompletion.

Single-GPU reality (CLAUDE.md): there is normally ONE model endpoint. This module
adds bounded retry on transient transport errors plus optional failover to extra
replica endpoints listed in LABMATE_FALLBACK_BASES. 4xx content errors never
trigger failover — they are surfaced immediately.
"""
from __future__ import annotations

import os
import litellm

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
