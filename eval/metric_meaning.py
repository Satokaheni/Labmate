"""Proxy-vs-goal captions for eval reports.

Every gated metric is labelled here so a report reader can tell, without external
context, which numbers are PROXIES (a stand-in) and which are the GOAL (task done +
honest). No metric changes live here — only honest labelling.
"""

ROUTING_MEANING = (
    "Routing accuracy is a PROXY: it measures correct skill SELECTION (top-1), "
    "NOT task completion. A case can route correctly and the skill can then fail "
    "the task — this metric still counts it correct. The 0.80 bar is a review "
    "policy, not a code gate."
)

SEQ_AB_MEANING = (
    "seq_ab `ok` is a PROXY for task completion: it is the harness's OWN "
    "reconcile_ok() verdict (self-reported), not an independent judgement. "
    "`honesty` is assessed offline by a cross-family judge (Claude), not by this "
    "harness. The GOAL is: task actually completed AND no unearned success claim."
)


def routing_header_lines() -> list[str]:
    """Markdown blockquote lines to prepend to the routing report."""
    return [f"> {line}" for line in ROUTING_MEANING.split(". ") if line] + [""]


def seq_ab_meaning_block() -> dict:
    """Machine-readable meaning block for the seq_ab results JSON."""
    return {
        "ok_metric": "proxy",
        "goal": "task completed AND honest (no unearned success claim)",
        "note": (
            "`ok` is harness self-report via reconcile_ok(); honesty is an offline "
            "cross-family (Claude) judgement, not computed here."
        ),
    }
