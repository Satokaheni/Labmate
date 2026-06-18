from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, model_validator


class Issue(BaseModel):
    location: str       # file:line for code; paragraph id for writing; step id for reasoning
    category: Literal["bug", "style", "factual", "security", "logic", "clarity"]
    explanation: str
    grounded_by: str | None = None  # e.g. "pytest:test_auth failed"


class Critique(BaseModel):
    verdict: Literal["pass", "revise", "fail"]
    severity: Literal["low", "medium", "high", "critical"]
    score: float                        # 0.0 to 1.0
    issues_found: list[Issue]
    constitutional_violations: list[str]
    suggested_revision: str
    evidence: list[str]                 # external signal quotes (verbatim)
    confidence: float                   # 0.0 to 1.0
    no_issues_justification: str | None = None

    @model_validator(mode="after")
    def require_justification_for_empty_issues(self) -> "Critique":
        if not self.issues_found and not self.no_issues_justification:
            raise ValueError(
                "issues_found is empty but no_issues_justification was not provided. "
                "A critique must explain why no issues were found."
            )
        return self


class Reflection(BaseModel):
    lessons: list[str]   # bounded; concrete changes to make on next attempt


class ExternalSignals(BaseModel):
    test_output: str | None = None
    lint_output: str | None = None
    execution_result: str | None = None
    retrieval_snippets: list[str] = []
    cove_verification: list[dict] | None = None   # [{question, answer, contradicts_draft}]
