"""Trivial baselines + leakage-inflation for the routing eval.

These give a reader a reference point next to the headline accuracy: is 0.80 good, or
does always-guess-the-commonest-skill already get 0.6? And is the number inflated by
evaluating on model-generated (leaked) cases?
"""

from collections import Counter


def majority_class_accuracy(cases: list[dict]) -> float:
    """Accuracy of always predicting the single most common `expected` label."""
    if not cases:
        return 0.0
    labels = [c["expected"] for c in cases]
    modal, _ = Counter(labels).most_common(1)[0]
    return sum(1 for x in labels if x == modal) / len(labels)


def random_baseline_accuracy(cases: list[dict], n_skills: int) -> float:
    """Expected accuracy of picking uniformly among the n_skills skills + 1 decline
    action. Each case has exactly one correct action, so P(correct) = 1/(n_skills+1)."""
    if not cases or n_skills < 0:
        return 0.0
    return 1.0 / (n_skills + 1)


def inflation_estimate(generated_overall: float, heldout_overall: float) -> float:
    """Leakage inflation = accuracy on model-generated cases minus accuracy on the
    held-out (non-model-generated) slice. Positive => the headline is inflated by cases
    that echo SKILL.md wording the router also sees."""
    return round(generated_overall - heldout_overall, 4)


def subset_overall(results: list[dict], cases: list[dict], source: str) -> float | None:
    """Accuracy over only the results whose case carries the given `source` tag.

    Returns None when no case has that source (the caller decides the fallback),
    so the leakage metric measures the model-generated slice specifically rather
    than the whole (mostly hand-written) working set.
    """
    ids = {c["id"] for c in cases if c.get("source") == source}
    subset = [r for r in results if r.get("id") in ids]
    if not subset:
        return None
    return sum(1 for r in subset if r.get("correct")) / len(subset)
