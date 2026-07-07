import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Make the skill package importable.
SKILL_DIR = Path(__file__).resolve().parents[4] / "services" / "skills" / "paper-rag"
sys.path.insert(0, str(SKILL_DIR))


@pytest.fixture
def mock_docs(monkeypatch):
    """Replace paperqa.Docs with a mock exposing async aadd/aquery/aget_evidence."""
    import paperqa

    docs = MagicMock()
    docs.aadd = AsyncMock(return_value="doc1")
    docs.aquery = AsyncMock(
        return_value=SimpleNamespace(
            answer="Photosynthesis converts light to energy (Smith2020).",
            contexts=[
                SimpleNamespace(
                    context="Light is converted to chemical energy.",
                    text=SimpleNamespace(
                        name="Smith2020", doc=SimpleNamespace(docname="Smith2020")
                    ),
                    score=5,
                )
            ],
        )
    )
    docs.aget_evidence = AsyncMock(
        return_value=SimpleNamespace(
            contexts=[
                SimpleNamespace(
                    context="Relevant passage.",
                    text=SimpleNamespace(
                        name="Smith2020", doc=SimpleNamespace(docname="Smith2020")
                    ),
                    score=4,
                )
            ]
        )
    )
    docs.docs = {
        "doc1": SimpleNamespace(
            title="A Paper", filepath="/tmp/a.pdf", docname="Smith2020", citation="Smith 2020"
        )
    }
    monkeypatch.setattr(paperqa, "Docs", MagicMock(return_value=docs))
    return docs
