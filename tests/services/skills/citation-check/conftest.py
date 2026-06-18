import sys
from pathlib import Path

import pytest

# Make the skill modules importable (they use flat imports).
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "services" / "skills" / "citation-check"))


@pytest.fixture
def patch_gemma(monkeypatch):
    """Patch claim_verifier._call_gemma to return scripted outputs."""
    import claim_verifier

    calls = {"responses": []}

    def fake(prompt: str) -> str:
        return calls["responses"].pop(0)

    monkeypatch.setattr(claim_verifier, "_call_gemma", fake)
    return calls
