import os
import sys

import pytest

# Make the skill package importable.
SKILL_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "../../../../services/skills/citation-graph")
)
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)


@pytest.fixture
def raw_paper():
    """A raw Semantic Scholar paper object (dict shape)."""
    return {
        "paperId": "abc123",
        "title": "Attention Is All You Need",
        "authors": [{"name": "Ashish Vaswani"}, {"name": "Noam Shazeer"}],
        "year": 2017,
        "externalIds": {"DOI": "10.5555/3295222.3295349", "ArXiv": "1706.03762"},
        "citationCount": 100000,
        "venue": "NeurIPS",
        "abstract": "The dominant sequence transduction models...",
        "tldr": {"text": "Proposes the Transformer architecture."},
        "openAccessPdf": {"url": "https://arxiv.org/pdf/1706.03762"},
    }


@pytest.fixture
def raw_citation_row():
    """A forward-citation wrapper row (citingPaper shape)."""
    return {
        "citingPaper": {
            "paperId": "cite1",
            "title": "BERT",
            "authors": [{"name": "Jacob Devlin"}],
            "year": 2018,
            "externalIds": {"DOI": "10.18653/v1/N19-1423"},
            "citationCount": 80000,
            "venue": "NAACL",
            "abstract": "We introduce BERT...",
            "tldr": None,
            "openAccessPdf": None,
        }
    }


@pytest.fixture
def raw_reference_row():
    """A backward-reference wrapper row (citedPaper shape)."""
    return {
        "citedPaper": {
            "paperId": "ref1",
            "title": "Neural Machine Translation",
            "authors": [{"name": "Dzmitry Bahdanau"}],
            "year": 2014,
            "externalIds": {"DOI": "10.0000/nmt"},
            "citationCount": 30000,
            "venue": "ICLR",
            "abstract": None,
            "tldr": None,
            "openAccessPdf": None,
        }
    }
