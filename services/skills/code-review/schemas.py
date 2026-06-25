from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, model_validator


class Candidate(BaseModel):
    file: str
    line: int | None = None
    summary: str
    failure_scenario: str
    severity: Literal["critical", "high", "medium", "low"]
    category: Literal["correctness", "security", "performance", "removed_behavior", "conventions"]
    angle: str  # which analysis angle surfaced this


class VerifiedFinding(BaseModel):
    file: str
    line: int | None = None
    summary: str
    failure_scenario: str
    severity: Literal["critical", "high", "medium", "low"]
    category: Literal["correctness", "security", "performance", "removed_behavior", "conventions"]
    confidence: Literal["confirmed", "plausible"]


class ScanResult(BaseModel):
    candidates: list[Candidate]

    @model_validator(mode="after")
    def cap_candidates(self) -> "ScanResult":
        self.candidates = self.candidates[:40]
        return self


class VerifyResult(BaseModel):
    findings: list[VerifiedFinding]


class ReviewResult(BaseModel):
    findings: list[VerifiedFinding]
    lint_issues: int
    angles_run: int
