from __future__ import annotations

import pytest

from services.orchestrator.lite_approval import requires_approval


@pytest.mark.parametrize(
    "t",
    [
        "deploy to prod",
        "delete the users table",
        "git push --force",
        "rm -rf build",
        "publish the release",
        "run a db migrate",
    ],
)
def test_irreversible_true(t):
    assert requires_approval(t) is True


@pytest.mark.parametrize(
    "t",
    ["review the file", "fix the bug", "what is 2+2?", "add a docstring", ""],
)
def test_reversible_false(t):
    assert requires_approval(t) is False
