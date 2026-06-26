"""Task complexity classifier — deterministic heuristics for conditional gates.

Classifies incoming tasks on two dimensions:
  - ambiguity_gate: trivial queries (facts, arithmetic, simple definitions) skip ambiguity checks
  - verify_gate: knowledge-only tasks skip artifact verification (code/writing do not skip)

The classifier is pure and deterministic (same task always produces same result).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Complexity:
    """Immutable complexity verdict for a task."""

    skip_ambiguity: bool  # True when the task is so trivial it can skip ambiguity gate
    skip_verify: bool     # True when the task needs no artifact verification
    reason: str           # Human-readable classification reason


def conditional_gates_enabled() -> bool:
    """Check whether conditional gates are enabled via env var.

    Returns False by default (feature is off).
    """
    return bool(os.environ.get("ENABLE_CONDITIONAL_GATES", ""))


def classify_complexity(
    task: str,
    *,
    enabled: bool | None = None,
) -> Complexity:
    """Classify a task's complexity to determine which gates to skip.

    Args:
        task: The incoming task description.
        enabled: Explicit feature toggle. If None, consults conditional_gates_enabled().
                If False, feature is disabled (returns Complexity with all False).

    Returns:
        Complexity dataclass with skip_* flags and a reason string.
    """
    # Determine if the feature is enabled
    if enabled is None:
        enabled = conditional_gates_enabled()

    # Feature disabled — skip nothing
    if not enabled:
        return Complexity(skip_ambiguity=False, skip_verify=False, reason="feature disabled")

    # Normalize: lowercase, strip whitespace
    normalized = task.lower().strip()

    # Pattern 1: Simple arithmetic/math queries
    # Examples: "What is 2+2?", "Calculate 10*5", "Compute the square root of 16"
    arithmetic_pattern = re.compile(
        r"(?:what is|calculate|compute|solve|what's|whats)\s*"
        r"(?:the\s*)?(?:answer to\s*)?"
        r"[\d\s\+\-\*\/\(\)\^√.]+\??",
        re.IGNORECASE,
    )
    if arithmetic_pattern.search(normalized):
        return Complexity(
            skip_ambiguity=True,
            skip_verify=True,
            reason="simple arithmetic query",
        )

    # Pattern 2: Simple fact lookups / definitions
    # Examples: "What is the capital of France?", "Who invented the telephone?"
    fact_pattern = re.compile(
        r"(?:what is|what's|whats|who|when|where|which|whose|how many)\s+(?:the\s+)?",
        re.IGNORECASE,
    )
    # Exclude if it looks multi-step (contains "and", "also", "additionally")
    if fact_pattern.search(normalized) and not re.search(
        r"\b(?:and|also|additionally|build|create|design|generate|write|code)\b",
        normalized,
    ):
        return Complexity(
            skip_ambiguity=True,
            skip_verify=True,
            reason="simple fact lookup",
        )

    # Pattern 3: Code/writing generation tasks
    # Examples: "Write a Python function", "Generate a SQL query", "Draft an email"
    # These skip ambiguity (clear intent) but NOT verify (code needs critique)
    code_patterns = re.compile(
        r"(?:write|generate|create|code|implement|build|design|draft|compose|script|make|develop)\s+(?:a\s+)?(?:python|javascript|typescript|java|c\+\+|rust|go|sql|html|css|bash|shell|function|class|method|algorithm|email|letter|doc|paragraph|story|poem)",
        re.IGNORECASE,
    )
    if code_patterns.search(normalized):
        return Complexity(
            skip_ambiguity=True,
            skip_verify=False,
            reason="code or writing task (needs verification)",
        )

    # Pattern 4: Simple web/document searches / lookups
    # Examples: "Find papers about X", "Search HuggingFace for datasets", "Look up articles on Y"
    # These skip ambiguity but NOT verify (results need to be checked)
    search_pattern = re.compile(
        r"(?:search|find|look\s+(?:up|for)|find\s+(?:papers|articles|research|datasets|data))",
        re.IGNORECASE,
    )
    if search_pattern.search(normalized):
        return Complexity(
            skip_ambiguity=True,
            skip_verify=False,
            reason="search/lookup task",
        )

    # Default: Complex task
    # Long tasks, multi-step instructions, design/architecture requests
    # do NOT skip either gate.
    return Complexity(
        skip_ambiguity=False,
        skip_verify=False,
        reason="complex task (requires ambiguity and verification gates)",
    )
