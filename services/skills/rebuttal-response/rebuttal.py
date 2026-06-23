"""rebuttal-response skill logic: parse reviews, draft replies, audit coverage.

CRITICAL: never write to stdout. All logging goes to stderr.
"""
from __future__ import annotations

import logging
import os
import re
import sys

import litellm

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(name)s %(levelname)s %(message)s")
log = logging.getLogger("rebuttal-response")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
_SECTION_RE = re.compile(r"section\s+(\d+(?:\.\d+)?)", re.IGNORECASE)


def parse_reviews(review_text: str) -> dict:
    """Decompose reviewer text into an itemized concern matrix."""
    concerns: list[dict] = []
    _BULLET_RE = re.compile(r"(?m)^\s*(?:\d+[.)]|[-*])\s+")
    if _BULLET_RE.search(review_text):
        chunks = _BULLET_RE.split(review_text)
    else:
        chunks = re.split(r"\n\s*\n", review_text)
    chunks = [c.strip() for c in chunks if c.strip()]
    for i, chunk in enumerate(chunks):
        low = chunk.lower()
        if "major" in low or "critical" in low or "weak" in low:
            severity = "major"
        else:
            severity = "minor"
        if "typo" in low or "grammar" in low or "wording" in low:
            ctype = "presentation"
        elif "experiment" in low or "baseline" in low or "evaluation" in low:
            ctype = "evaluation"
        elif "cite" in low or "reference" in low or "related work" in low:
            ctype = "related_work"
        else:
            ctype = "general"
        sec_match = _SECTION_RE.search(chunk)
        target = sec_match.group(1) if sec_match else ""
        concerns.append({
            "id": f"c{i + 1}",
            "severity": severity,
            "type": ctype,
            "target_section": target,
            "text": chunk,
        })
    return {"concerns": concerns}


async def draft_response(concerns: list[dict], paper_context: str) -> dict:
    """Generate a point-by-point reply per concern via Gemma 4 31B."""
    responses: list[dict] = []
    for concern in concerns:
        prompt = (
            "You are drafting an author response to a peer-review concern.\n"
            f"PAPER CONTEXT:\n{paper_context}\n\n"
            f"REVIEWER CONCERN ({concern.get('severity', 'minor')}, "
            f"{concern.get('type', 'general')}): {concern.get('text', '')}\n\n"
            "Write a concise, respectful, point-by-point reply grounded in the paper. "
            "Do not invent results not in the context."
        )
        try:
            r = await litellm.acompletion(
                model="openai/gemma-4-31b",
                api_base=GEMMA_BASE,
                api_key="not-needed",
                messages=[{"role": "user", "content": prompt}],
                extra_body={"thinking_budget_tokens": 1024},
            )
            text = r.choices[0].message.content or ""
        except Exception as exc:
            log.warning("draft_response failed for %s: %s", concern.get("id"), exc)
            text = ""
        responses.append({"concern_id": concern.get("id", ""), "response": text})
    return {"responses": responses}


def coverage_audit(concerns: list[dict], responses: list[dict]) -> dict:
    """Confirm every concern is addressed; flag the unaddressed ones."""
    concern_ids = [c.get("id") for c in concerns if c.get("id")]
    answered = {r.get("concern_id") for r in responses
                if r.get("concern_id") and (r.get("response") or "").strip()}
    covered = [cid for cid in concern_ids if cid in answered]
    gaps = [cid for cid in concern_ids if cid not in answered]
    total = len(concern_ids)
    pct = (len(covered) / total) if total else 1.0
    return {"covered": covered, "gaps": gaps, "coverage_pct": pct}
