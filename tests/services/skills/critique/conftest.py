from __future__ import annotations

import pytest

from services.skills.critique.schemas import Critique, Issue


class MockLM:
    """Scriptable stand-in for an instructor-wrapped LLM client.

    chat_returns: queue of Critique objects returned by .chat()
    complete_returns: queue of strings returned by .complete()
    """

    def __init__(self, chat_returns=None, complete_returns=None):
        self._chat_returns = list(chat_returns or [])
        self._complete_returns = list(complete_returns or [])
        self.chat_calls = []
        self.complete_calls = []

    def chat(self, response_model=None, messages=None, **kwargs):
        self.chat_calls.append({"messages": messages, "kwargs": kwargs})
        return self._chat_returns.pop(0)

    def complete(self, prompt: str) -> str:
        self.complete_calls.append(prompt)
        if self._complete_returns:
            return self._complete_returns.pop(0)
        return ""


@pytest.fixture
def make_issue():
    def _make(category="bug", location="f.py:1", explanation="x", grounded_by=None):
        return Issue(
            location=location, category=category,
            explanation=explanation, grounded_by=grounded_by,
        )
    return _make


@pytest.fixture
def make_critique(make_issue):
    def _make(
        verdict="revise", severity="medium", score=0.5,
        issues_found=None, constitutional_violations=None,
        suggested_revision="fix it", evidence=None, confidence=0.7,
        no_issues_justification=None,
    ):
        return Critique(
            verdict=verdict, severity=severity, score=score,
            issues_found=issues_found if issues_found is not None else [make_issue()],
            constitutional_violations=constitutional_violations or [],
            suggested_revision=suggested_revision,
            evidence=evidence or [],
            confidence=confidence,
            no_issues_justification=no_issues_justification,
        )
    return _make
