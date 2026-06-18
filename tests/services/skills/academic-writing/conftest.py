import os
import sys
import types

import pytest

SKILL_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..",
                 "services", "skills", "academic-writing")
)
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)


@pytest.fixture
def fake_lm():
    """A dummy dspy.LM stand-in; the DSPy modules are monkeypatched per test."""
    class _FakeLM:
        def __call__(self, *a, **k):
            return [""]
    return _FakeLM()


@pytest.fixture
def make_ref():
    from academic_writing_skill import Ref

    def _make(key, title="A Title", abstract="An abstract."):
        return Ref(id=key, title=title, abstract=abstract,
                   bibtex=f"@article{{{key},\n  title = {{{title}}}\n}}")
    return _make


class FakeCrossref:
    def __init__(self, response=None, raise_exc=False):
        self._response = response
        self._raise = raise_exc

    def works(self, ids=None):
        if self._raise:
            raise RuntimeError("crossref down")
        return self._response


class FakeHit:
    def __init__(self, title, authors, year=2024):
        self.title = title
        self.authors = [{"name": n} for n in authors]
        self.year = year


class FakeSemanticScholar:
    def __init__(self, hits=None, raise_exc=False):
        self._hits = hits or []
        self._raise = raise_exc

    def search_paper(self, title, limit=3):
        if self._raise:
            raise RuntimeError("ss down")
        return self._hits
