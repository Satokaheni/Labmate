"""ClaimVerifier — RefChecker (arXiv:2405.14486) claim-triplet extraction
and 3-way verification against reference passages."""
from __future__ import annotations

import json
import logging
import os
import sys

import litellm

from models import ClaimTriplet, ClaimVerificationResult

# All diagnostics to stderr — stdout is reserved for JSON-RPC.
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("citation-check.claim")

GEMMA_BASE = os.getenv("GEMMA_BASE", "http://localhost:8000/v1")
QWEN_BASE = os.getenv("QWEN_BASE", GEMMA_BASE)
GEMMA_MODEL = os.getenv("GEMMA_MODEL", "google/gemma-4-31B-it")

EXTRACTION_PROMPT = """You are a claim extractor. Decompose the TEXT below into atomic \
knowledge claims. Each claim is a (subject, predicate, object) triplet capturing one \
verifiable fact. Do not infer beyond the text. Return ONLY a JSON array, e.g.:
[{{"subject": "BERT", "predicate": "was introduced by", "object": "Devlin et al. 2018"}}]

TEXT:
{text}
"""


def _call_gemma(prompt: str) -> str:
    resp = litellm.completion(
        model=f"openai/{GEMMA_MODEL}",
        api_base=GEMMA_BASE,
        api_key="not-needed",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
    )
    return resp["choices"][0]["message"]["content"]


def _parse_json_array(raw: str) -> list[dict]:
    """Tolerant parse: strip code fences, locate the first JSON array."""
    s = raw.strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
    start, end = s.find("["), s.rfind("]")
    if start == -1 or end == -1:
        log.warning("no JSON array found in extraction output")
        return []
    try:
        return json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        log.warning("failed to parse extraction JSON")
        return []


def _extract_triplets(text: str) -> list[dict]:
    raw = _call_gemma(EXTRACTION_PROMPT.format(text=text))
    return _parse_json_array(raw)


CLASSIFY_PROMPT = """Given the REFERENCES and a single CLAIM triplet, decide whether the \
references support the claim. Answer with strict JSON:
{{"verdict": "entailed|contradicted|unverifiable", "evidence": "<quoted passage or null>"}}

- "entailed": the references clearly support the claim.
- "contradicted": the references clearly state the opposite.
- "unverifiable": the references neither support nor contradict it.

REFERENCES:
{references}

CLAIM: subject="{subject}" predicate="{predicate}" object="{object}"
"""


def _classify_triplet(triplet: dict, references_blob: str) -> ClaimTriplet:
    prompt = CLASSIFY_PROMPT.format(
        references=references_blob,
        subject=triplet.get("subject", ""),
        predicate=triplet.get("predicate", ""),
        object=triplet.get("object", ""),
    )
    raw = _call_gemma(prompt)
    verdict, evidence = "unverifiable", None
    try:
        s = raw[raw.find("{") : raw.rfind("}") + 1]
        parsed = json.loads(s)
        v = parsed.get("verdict")
        if v in ("entailed", "contradicted", "unverifiable"):
            verdict = v
        evidence = parsed.get("evidence") or None
    except (json.JSONDecodeError, ValueError):
        log.warning("failed to parse classification JSON; defaulting unverifiable")
    return ClaimTriplet(
        subject=triplet.get("subject", ""),
        predicate=triplet.get("predicate", ""),
        object=triplet.get("object", ""),
        verdict=verdict,
        evidence=evidence,
    )


def verify_claims(text: str, references: list[str]) -> ClaimVerificationResult:
    references_blob = "\n\n".join(references) if references else ""
    raw_triplets = _extract_triplets(text)
    triplets: list[ClaimTriplet] = []
    for rt in raw_triplets:
        if not references_blob:
            triplets.append(
                ClaimTriplet(
                    subject=rt.get("subject", ""),
                    predicate=rt.get("predicate", ""),
                    object=rt.get("object", ""),
                    verdict="unverifiable",
                )
            )
            continue
        triplets.append(_classify_triplet(rt, references_blob))
    return ClaimVerificationResult.from_triplets(text, triplets)
