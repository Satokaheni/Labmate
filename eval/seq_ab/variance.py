"""Closed-form Wilson score interval for a binomial pass-rate, and a disjoint test.

Wilson is used (not normal-approx) because n is tiny (TRIALS>=3): it stays inside
[0,1] and never collapses to a zero-width interval at 0/n or n/n.
"""

import math


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95%-by-default Wilson score interval for `successes` out of `n`.

    n == 0 -> (0.0, 1.0) (no information). Result is clamped to [0, 1].
    """
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def intervals_disjoint(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """True iff intervals a and b do not overlap (touching endpoints count as overlap)."""
    return a[1] < b[0] or b[1] < a[0]
