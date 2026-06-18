"""Shared Pydantic models for the citation-check skill."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class ClaimTriplet(BaseModel):
    subject: str
    predicate: str
    object: str
    verdict: Literal["entailed", "contradicted", "unverifiable"]
    evidence: str | None = None  # quoted passage from reference that entails/contradicts


class ClaimVerificationResult(BaseModel):
    text: str
    triplets: list[ClaimTriplet]
    entailed_count: int
    contradicted_count: int
    unverifiable_count: int

    @classmethod
    def from_triplets(cls, text: str, triplets: list[ClaimTriplet]) -> "ClaimVerificationResult":
        return cls(
            text=text,
            triplets=triplets,
            entailed_count=sum(1 for t in triplets if t.verdict == "entailed"),
            contradicted_count=sum(1 for t in triplets if t.verdict == "contradicted"),
            unverifiable_count=sum(1 for t in triplets if t.verdict == "unverifiable"),
        )


class CitationCheckResult(BaseModel):
    entry_id: str
    verdict: Literal["exact_match", "minor_hallucination", "major_hallucination"]
    field_errors: list[str] = []  # specific corrupted fields for minor_hallucination
    source: str | None = None  # 'crossref' | 'semantic_scholar' | 'arxiv'
    normalized_bibtex: str | None = None
