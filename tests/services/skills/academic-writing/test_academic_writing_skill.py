import json

import pytest

import academic_writing_skill as aw
from academic_writing_skill import AcademicWritingSkill, IMRAD_ORDER


class _Pred:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _skill_with_outline(monkeypatch, outline_json):
    monkeypatch.setattr(aw.dspy, "configure", lambda **k: None)
    skill = AcademicWritingSkill.__new__(AcademicWritingSkill)
    skill._outline_module = lambda topic, references: _Pred(outline=outline_json)
    return skill


def test_outline_skill_raises_on_missing_section(monkeypatch, make_ref):
    outline_json = json.dumps({"sections": [
        {"name": "Introduction", "ref_ids": [], "key_points": [], "word_budget": 400},
        {"name": "Results", "ref_ids": [], "key_points": [], "word_budget": 400},
        {"name": "Discussion", "ref_ids": [], "key_points": [], "word_budget": 400},
        {"name": "Conclusion", "ref_ids": [], "key_points": [], "word_budget": 200},
    ]})
    skill = _skill_with_outline(monkeypatch, outline_json)
    with pytest.raises(ValueError, match="missing required IMRaD sections"):
        skill.outline_skill("topic", [make_ref("a2024")])


def test_outline_skill_sorts_canonical_order(monkeypatch, make_ref):
    outline_json = json.dumps({"sections": [
        {"name": "Discussion", "ref_ids": [], "key_points": [], "word_budget": 400},
        {"name": "Introduction", "ref_ids": [], "key_points": [], "word_budget": 400},
        {"name": "Conclusion", "ref_ids": [], "key_points": [], "word_budget": 200},
        {"name": "Methods", "ref_ids": [], "key_points": [], "word_budget": 400},
        {"name": "Background", "ref_ids": [], "key_points": [], "word_budget": 400},
        {"name": "Experimental Setup", "ref_ids": [], "key_points": [], "word_budget": 300},
        {"name": "Results", "ref_ids": [], "key_points": [], "word_budget": 400},
    ]})
    skill = _skill_with_outline(monkeypatch, outline_json)
    outline = skill.outline_skill("topic", [make_ref("a2024")])
    names = [s.name for s in outline.sections]
    order_map = {n: i for i, n in enumerate(IMRAD_ORDER)}
    assert names == sorted(names, key=lambda n: order_map[n])
    assert names[0] == "Introduction"


def test_draft_section_rejects_unvalidated_key(monkeypatch, make_ref):
    monkeypatch.setattr(aw.dspy, "configure", lambda **k: None)
    skill = AcademicWritingSkill.__new__(AcademicWritingSkill)
    skill._section_module = lambda **k: _Pred(
        section_text="As shown \\cite{ghost2099}, this works."
    )
    refs = [make_ref("real2024")]
    with pytest.raises(ValueError, match="unvalidated keys"):
        skill.draft_section("Methods", refs, notes="notes")


def test_draft_section_accepts_valid_key(monkeypatch, make_ref):
    monkeypatch.setattr(aw.dspy, "configure", lambda **k: None)
    skill = AcademicWritingSkill.__new__(AcademicWritingSkill)
    skill._section_module = lambda **k: _Pred(
        section_text="As shown \\cite{real2024}, this works."
    )
    out = skill.draft_section("Methods", [make_ref("real2024")], notes="notes")
    assert "\\cite{real2024}" in out


def _make_cod_skill(monkeypatch, sparse_text, densify_texts, missing_lists):
    monkeypatch.setattr(aw.dspy, "configure", lambda **k: None)
    skill = AcademicWritingSkill.__new__(AcademicWritingSkill)

    class _FakeCoD:
        def __init__(self):
            self._densify_calls = 0
            self._missing_calls = 0

        def initial_sparse(self, text, target_words):
            return sparse_text

        def identify_missing(self, text, summary):
            i = self._missing_calls
            self._missing_calls += 1
            return missing_lists[i] if i < len(missing_lists) else []

        def densify(self, summary, missing, target_words):
            i = self._densify_calls
            self._densify_calls += 1
            return densify_texts[i]

    skill._cod_module = _FakeCoD()
    return skill


def test_cod_first_iteration_is_sparse(monkeypatch):
    target = 20
    sparse = " ".join(["word"] * target)  # exactly target words, no entity jamming
    skill = _make_cod_skill(
        monkeypatch,
        sparse_text=sparse,
        densify_texts=[" ".join(["word"] * target)],
        missing_lists=[["EntityA"], []],
    )
    # Capture the seed directly: it must be at the target, not exceeding it.
    seed = skill._cod_module.initial_sparse("source", target)
    assert len(seed.split()) <= target


def test_cod_word_count_never_increases(monkeypatch):
    target = 20
    sparse = " ".join(["w"] * target)
    # Each densify deliberately overruns; _enforce_word_count must truncate back.
    densify_over = [" ".join(["w"] * (target + 30)), " ".join(["w"] * (target + 50))]
    skill = _make_cod_skill(
        monkeypatch,
        sparse_text=sparse,
        densify_texts=densify_over,
        missing_lists=[["A"], ["B"], []],
    )
    out = skill.chain_of_density("source", target_words=target, iterations=3)
    assert len(out.split()) <= target


def test_style_transfer_rejects_altered_cite(monkeypatch):
    monkeypatch.setattr(aw.dspy, "configure", lambda **k: None)
    skill = AcademicWritingSkill.__new__(AcademicWritingSkill)
    skill._tst_module = lambda **k: _Pred(
        transferred_text="Formal prose with no citation and number 42."
    )
    src = "casual prose \\cite{smith2024} with number 42."
    with pytest.raises(ValueError, match="altered after 2 attempts"):
        skill.style_transfer(src)


def test_style_transfer_passes_when_tokens_preserved(monkeypatch):
    monkeypatch.setattr(aw.dspy, "configure", lambda **k: None)
    skill = AcademicWritingSkill.__new__(AcademicWritingSkill)
    skill._tst_module = lambda **k: _Pred(
        transferred_text="Formal prose \\cite{smith2024} with the value 42."
    )
    src = "casual \\cite{smith2024} with 42."
    out = skill.style_transfer(src)
    assert "\\cite{smith2024}" in out and "42" in out
