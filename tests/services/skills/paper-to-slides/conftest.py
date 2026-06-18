import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[4] / "services" / "skills" / "paper-to-slides"
sys.path.insert(0, str(SKILL))


@pytest.fixture
def parsed_paper():
    return {
        "markdown": "# Intro\nWe study X.\n# Methods\nWe do Y.\n# Results\nZ works.",
        "figures": [{"path": "/tmp/fig1.png", "caption": "Architecture", "page": 2}],
        "tables": [{"html": "<table></table>", "caption": "Scores", "page": 4}],
        "metadata": {"title": "On X", "authors": ["A. Author"], "doi": "10.0/x",
                     "page_count": 8},
    }


def _llm_json(obj):
    import json
    return {"choices": [{"message": {"content": json.dumps(obj)}}]}


@pytest.fixture
def llm_json():
    return _llm_json
