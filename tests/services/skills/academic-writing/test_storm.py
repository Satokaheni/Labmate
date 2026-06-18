import json

import pytest

import academic_writing_skill as aw
from academic_writing_skill import AcademicWritingSkill

pytestmark = pytest.mark.mocked


class _Pred:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _storm_skill(monkeypatch, perspectives_json, answer_text, notes_json):
    """Build an AcademicWritingSkill with all three STORM modules stubbed."""
    monkeypatch.setattr(aw.dspy, "configure", lambda **k: None)
    skill = AcademicWritingSkill.__new__(AcademicWritingSkill)
    skill._perspective_module = lambda topic, n_perspectives: _Pred(perspectives=perspectives_json)
    skill._interview_module = lambda question, references: _Pred(answer=answer_text)
    skill._synthesis_module = lambda topic, exchanges: _Pred(notes=notes_json)
    return skill


def _perspectives(n):
    return json.dumps([
        {"role": f"Expert {i}", "focus": f"focus {i}", "questions": [f"q{i}a", f"q{i}b"]}
        for i in range(n)
    ])


_NOTES = json.dumps({
    "key_findings": ["Finding one.", "Finding two."],
    "suggested_sections": ["Methods", "Results"],
})


def test_research_topic_returns_requested_perspectives(monkeypatch, make_ref):
    skill = _storm_skill(
        monkeypatch,
        perspectives_json=_perspectives(3),
        answer_text="Grounded answer [r1].",
        notes_json=_NOTES,
    )
    notes = skill.research_topic("a topic", [make_ref("r1")], n_perspectives=3)
    assert len(notes.perspectives) == 3
    assert notes.topic == "a topic"


def test_each_perspective_has_questions(monkeypatch, make_ref):
    skill = _storm_skill(
        monkeypatch,
        perspectives_json=_perspectives(3),
        answer_text="Grounded answer [r1].",
        notes_json=_NOTES,
    )
    notes = skill.research_topic("a topic", [make_ref("r1")])
    for p in notes.perspectives:
        assert p.questions, f"perspective {p.role!r} had no questions"


def test_key_findings_non_empty(monkeypatch, make_ref):
    skill = _storm_skill(
        monkeypatch,
        perspectives_json=_perspectives(2),
        answer_text="Grounded answer [r1].",
        notes_json=_NOTES,
    )
    notes = skill.research_topic("a topic", [make_ref("r1")])
    assert notes.key_findings
    assert all(isinstance(f, str) and f for f in notes.key_findings)


def test_exchanges_only_cite_supplied_refs(monkeypatch, make_ref):
    skill = _storm_skill(
        monkeypatch,
        perspectives_json=_perspectives(1),
        answer_text="Per [r1] this holds, unlike [ghost9999].",
        notes_json=_NOTES,
    )
    refs = [make_ref("r1")]
    notes = skill.research_topic("a topic", refs)
    assert notes.exchanges
    allowed = {r.id for r in refs}
    for ex in notes.exchanges:
        assert set(ex.cited_refs) <= allowed
        assert "ghost9999" not in ex.cited_refs


def _full_outline_json():
    sections = [
        {"name": n, "ref_ids": [], "key_points": [], "word_budget": 300}
        for n in ["Introduction", "Background", "Methods",
                  "Experimental Setup", "Results", "Discussion", "Conclusion"]
    ]
    return json.dumps({"sections": sections})


def test_findings_feed_outline_skill(monkeypatch, make_ref):
    skill = _storm_skill(
        monkeypatch,
        perspectives_json=_perspectives(2),
        answer_text="Grounded [r1].",
        notes_json=_NOTES,
    )
    # Stub the outline module on the same instance.
    skill._outline_module = lambda topic, references: _Pred(outline=_full_outline_json())

    refs = [make_ref("r1")]
    notes = skill.research_topic("a topic", refs)
    outline = skill.outline_skill("a topic", refs)

    for section in outline.sections:
        section.key_points.extend(notes.key_findings)

    assert all(set(notes.key_findings) <= set(s.key_points) for s in outline.sections)


def test_research_topic_raises_on_unparseable_perspectives(monkeypatch, make_ref):
    skill = _storm_skill(
        monkeypatch,
        perspectives_json="I could not produce JSON, sorry.",
        answer_text="x",
        notes_json=_NOTES,
    )
    with pytest.raises(ValueError, match="no JSON array"):
        skill.research_topic("a topic", [make_ref("r1")])
