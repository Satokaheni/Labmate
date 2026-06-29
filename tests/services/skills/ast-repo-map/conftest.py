import sys
from pathlib import Path

import pytest

SERVER_DIR = (
    Path(__file__).resolve().parents[4]
    / "services" / "skills" / "ast-repo-map"
)
sys.path.insert(0, str(SERVER_DIR))


class _FakeTokenizer:
    """Deterministic stand-in for the Gemma tokenizer.

    One token per whitespace-separated chunk. Avoids downloading
    google/gemma-4-31B-it in CI while preserving budget semantics.
    """

    def encode(self, text: str) -> list[int]:
        return list(range(len(text.split())))


@pytest.fixture
def repo_mapper(tmp_path):
    import repo_mapper as rm
    rm.RepoMapper._tokenizer = _FakeTokenizer()  # bypass transformers load
    return rm


@pytest.fixture
def sample_repo(tmp_path):
    """A tiny multi-file Python repo with cross-file references."""
    (tmp_path / "util.py").write_text(
        "def helper():\n    return 1\n\n"
        "class Widget:\n    def build(self):\n        return helper()\n"
    )
    (tmp_path / "service.py").write_text(
        "from util import helper, Widget\n\n"
        "def run():\n    w = Widget()\n    return helper() + w.build()\n"
    )
    return tmp_path
