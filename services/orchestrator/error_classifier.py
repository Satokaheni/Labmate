# services/orchestrator/error_classifier.py
"""Pure, deterministic, case-insensitive classification of failure strings.

Replaces graph.py's crude _NONRETRYABLE_ERROR_MARKERS substring tuple. On a
single-GPU pod most skill failures are ENVIRONMENTAL/terminal (no Docker, no
API key, no network) and reflect-retrying them to exhaustion is the documented
#1 latency sink. The orchestrator uses the returned ErrorClass to decide
whether to reflect-retry, back off, or finalize immediately.

This module is PURE: no I/O, no env reads, no logging. classify_error() is a
total function over (str | Exception | None).
"""
from __future__ import annotations

from enum import Enum


class ErrorClass(str, Enum):
    """Mutually-exclusive failure categories. .value == .name for JSON safety."""
    TERMINAL_DEPENDENCY = "TERMINAL_DEPENDENCY"   # missing tool/sandbox/skill/docker
    TERMINAL_CREDENTIAL = "TERMINAL_CREDENTIAL"   # missing/invalid api key or auth
    TERMINAL_NETWORK = "TERMINAL_NETWORK"         # connection refused / DNS / unreachable
    RATE_LIMITED = "RATE_LIMITED"                 # 429 — bounded backoff then terminal
    TRANSIENT = "TRANSIENT"                       # timeouts — limited retry
    RETRYABLE = "RETRYABLE"                       # default / unknown — normal retry budget


TERMINAL_CLASSES: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.TERMINAL_DEPENDENCY,
        ErrorClass.TERMINAL_CREDENTIAL,
        ErrorClass.TERMINAL_NETWORK,
    }
)

# Ordered list of (ErrorClass, substrings). ORDER MATTERS: the first class with
# any matching substring wins. Order encodes precedence decisions:
#   1. RATE_LIMITED first — a 429 is rate limiting even if it mentions the host
#      ("429 ... from connection pool"); it must not be swallowed by NETWORK.
#   2. TERMINAL_CREDENTIAL before TERMINAL_NETWORK — a 401/403 over a working
#      connection is an auth problem, not a network one.
#   3. TERMINAL_NETWORK before TRANSIENT — "connection refused" is terminal even
#      though it is networky; only pure timeouts are TRANSIENT.
#   4. TERMINAL_DEPENDENCY catches missing tools/skills/sandbox.
#   5. TRANSIENT for bare timeouts.
# Every substring is lower-case; matching is done against the lower-cased input.
# All legacy _NONRETRYABLE_ERROR_MARKERS substrings are preserved below.
_PATTERNS: tuple[tuple[ErrorClass, tuple[str, ...]], ...] = (
    (
        ErrorClass.RATE_LIMITED,
        ("429", "rate limit", "rate-limit", "too many requests"),
    ),
    (
        ErrorClass.TERMINAL_CREDENTIAL,
        (
            "api key", "api_key", "apikey", "credential",
            "unauthorized", "401", "403", "forbidden",
            "authentication failed", "auth failed",
        ),
    ),
    (
        ErrorClass.TERMINAL_NETWORK,
        (
            "connection refused", "econnrefused", "network is unreachable",
            "name resolution", "enotfound", "getaddrinfo", "network",
            "dns",
        ),
    ),
    (
        ErrorClass.TERMINAL_DEPENDENCY,
        (
            "skillunavailable", "unavailable", "not available", "no such",
            "not found", "missing", "docker", "unshare", "eperm", "enoent",
            "nsjail", "bwrap", "gvisor", "sandbox", "permission denied",
        ),
    ),
    (
        ErrorClass.TRANSIENT,
        ("timed out", "timeout"),
    ),
)


def classify_error(error: "str | Exception | None") -> ErrorClass:
    """Classify a failure into exactly one ErrorClass.

    Pure, case-insensitive, deterministic, total. Unknown/empty -> RETRYABLE
    (preserving the legacy default: retry up to MAX_GOAL_ATTEMPTS).
    """
    if error is None:
        return ErrorClass.RETRYABLE
    text = str(error).strip()
    if not text:
        return ErrorClass.RETRYABLE
    low = text.lower()
    for cls, substrings in _PATTERNS:
        if any(s in low for s in substrings):
            return cls
    return ErrorClass.RETRYABLE


def is_terminal(cls: ErrorClass) -> bool:
    """True for the three terminal classes (never reflect-retry these)."""
    return cls in TERMINAL_CLASSES
