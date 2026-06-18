# knowledge-curation (STORM Pattern) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend AcademicWritingSkill with STORM-style perspective-guided research_topic() — a pre-writing phase that simulates expert interviews to generate structured research notes feeding outline_skill().

**Architecture:** Three new DSPy modules (PerspectiveGenerator, ExpertInterviewer, ResearchNotesSynthesizer) implement the STORM pattern: generate diverse expert perspectives → simulate Q&A against reference material → synthesize into ResearchNotes. The output feeds directly into outline_skill()'s key_points. No new MCP server — this extends the existing academic-writing Python module.

**Tech Stack:** Python 3.11+, `dspy-ai` (already in academic-writing requirements), `pydantic>=2`, `pytest`

---

## Prerequisites

This plan **extends** the AcademicWritingSkill built by `2026-06-17-academic-writing-skill.md`.
Those files must already exist before starting:

- `services/skills/academic-writing/academic_writing_skill.py` (defines `Ref`, `Section`, `Outline`, `AcademicWritingSkill`, `outline_skill`)
- `services/skills/academic-writing/dspy_modules.py`
- `services/skills/academic-writing/SKILL.md`
- `tests/services/skills/academic-writing/conftest.py` (defines `make_ref`, `SKILL_DIR` path insertion)

Confirm before starting:

```bash
cd /Users/zachstallbohm/Work/gemma
test -f services/skills/academic-writing/academic_writing_skill.py \
  && test -f services/skills/academic-writing/dspy_modules.py \
  && echo "prereqs present" || echo "MISSING — build 2026-06-17-academic-writing-skill.md first"
```

---

## Phase 0 — STORM DSPy modules (`storm_modules.py`)

### Task 0.1 — Create `storm_modules.py` with data types

- [ ] Create `services/skills/academic-writing/storm_modules.py` with the module header
  and the three dataclasses. These are the typed contract the skill method returns.

```python
"""STORM-pattern pre-writing research modules for the academic writing pipeline.

STORM's core insight: high-quality long-form writing requires asking the right
questions first. The pre-writing phase simulates diverse expert perspectives and
"interviews" them against the supplied reference material, then synthesizes the
exchanges into structured research notes that seed the outline.

All LLM calls go through DSPy modules (consistent with the rest of the skill) so
prompts can be optimized without touching skill code.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import dspy


@dataclass
class Perspective:
    role: str              # e.g. "Machine learning researcher focused on efficiency"
    focus: str             # what aspect of the topic they care about
    questions: list[str] = field(default_factory=list)


@dataclass
class InterviewExchange:
    question: str
    answer: str            # grounded in refs
    cited_refs: list[str] = field(default_factory=list)  # ref ids used in the answer


@dataclass
class ResearchNotes:
    topic: str
    perspectives: list[Perspective]
    exchanges: list[InterviewExchange]
    key_findings: list[str]        # synthesized; feeds outline_skill's key_points
    suggested_sections: list[str]  # section-name suggestions
```

### Task 0.2 — Add the perspective-generation signature and module

- [ ] Append the `GeneratePerspectives` signature and `PerspectiveGenerator` module to
  `storm_modules.py`. The module returns the raw JSON string; parsing happens in the skill.

```python
class GeneratePerspectives(dspy.Signature):
    """Generate N distinct expert perspectives on a research topic.
    Each perspective should represent a different viewpoint: methodology, application,
    theory, critique. Avoid overlapping roles.
    Output JSON: [{"role": str, "focus": str, "questions": [str, ...]}]
    Each perspective must include 2-4 specific, answerable questions about the topic.
    """

    topic = dspy.InputField()
    n_perspectives = dspy.InputField(desc="number of perspectives to generate")
    perspectives = dspy.OutputField(desc="JSON array of perspective objects")


class PerspectiveGenerator(dspy.Module):
    def __init__(self):
        self.gen = dspy.ChainOfThought(GeneratePerspectives)

    def forward(self, topic, n_perspectives) -> dspy.Prediction:
        return self.gen(topic=topic, n_perspectives=str(n_perspectives))
```

### Task 0.3 — Add the interview-simulation signature and module

- [ ] Append the `SimulateInterview` signature and `ExpertInterviewer` module to
  `storm_modules.py`. The answer must be grounded only in the supplied references.

```python
class SimulateInterview(dspy.Signature):
    """Answer a research question as an expert, grounded ONLY in the supplied references.
    Cite specific references by their id using inline [ref_id] markers.
    Do NOT invent facts not present in the references. If the references do not cover
    the question, say so explicitly rather than fabricating an answer.
    """

    question = dspy.InputField()
    references = dspy.InputField(desc="list of {id, title, abstract}")
    answer = dspy.OutputField(desc="grounded answer with [ref_id] inline citations")


class ExpertInterviewer(dspy.Module):
    def __init__(self):
        self.gen = dspy.ChainOfThought(SimulateInterview)

    def forward(self, question, references) -> dspy.Prediction:
        return self.gen(question=question, references=str(references))
```

### Task 0.4 — Add the notes-synthesis signature and module

- [ ] Append the `SynthesizeNotes` signature and `ResearchNotesSynthesizer` module to
  `storm_modules.py`. Returns the raw JSON string; parsing happens in the skill.

```python
class SynthesizeNotes(dspy.Signature):
    """Synthesize research exchanges into structured notes for paper writing.
    key_findings are concise, self-contained claims supported by the exchanges; they
    will become the key_points of the paper outline.
    suggested_sections are candidate IMRaD-compatible section names implied by the findings.
    Output JSON: {"key_findings": [str, ...], "suggested_sections": [str, ...]}
    """

    topic = dspy.InputField()
    exchanges = dspy.InputField(desc="list of {question, answer}")
    notes = dspy.OutputField(desc="JSON synthesis object")


class ResearchNotesSynthesizer(dspy.Module):
    def __init__(self):
        self.gen = dspy.ChainOfThought(SynthesizeNotes)

    def forward(self, topic, exchanges) -> dspy.Prediction:
        return self.gen(topic=topic, exchanges=str(exchanges))
```

---

## Phase 1 — Skill integration (`academic_writing_skill.py`)

### Task 1.1 — Import the STORM modules and types

- [ ] Add the STORM imports near the existing `from dspy_modules import (...)` block in
  `services/skills/academic-writing/academic_writing_skill.py`.

```python
from storm_modules import (
    ExpertInterviewer,
    InterviewExchange,
    Perspective,
    PerspectiveGenerator,
    ResearchNotes,
    ResearchNotesSynthesizer,
)
```

### Task 1.2 — Add STORM JSON parsing helpers

- [ ] Append the two parse helpers and the cited-ref extractor to the module-level helper
  section of `academic_writing_skill.py` (alongside `_parse_outline_json`). They tolerate
  prose around the JSON and clamp cited refs to the supplied allowlist.

```python
def _parse_perspectives_json(raw: str) -> list[Perspective]:
    """Parse the PerspectiveGenerator JSON array into Perspective objects.

    Tolerates leading/trailing prose around the JSON array.
    """
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        raise ValueError(f"Perspectives output contained no JSON array: {raw[:200]!r}")
    data = json.loads(m.group(0))
    perspectives = []
    for p in data:
        perspectives.append(
            Perspective(
                role=p["role"],
                focus=p.get("focus", ""),
                questions=list(p.get("questions", [])),
            )
        )
    return perspectives


def _parse_notes_json(raw: str) -> tuple[list[str], list[str]]:
    """Parse the SynthesizeNotes JSON object into (key_findings, suggested_sections)."""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError(f"Notes output contained no JSON object: {raw[:200]!r}")
    data = json.loads(m.group(0))
    key_findings = list(data.get("key_findings", []))
    suggested_sections = list(data.get("suggested_sections", []))
    return key_findings, suggested_sections


def _extract_cited_refs(answer: str, allowed_ids: set[str]) -> list[str]:
    """Collect [ref_id] markers in the answer, keeping only ids in the allowlist.

    Prevents the interviewer from "citing" refs that were never supplied.
    """
    found = re.findall(r"\[([^\]]+)\]", answer)
    return [rid.strip() for rid in found if rid.strip() in allowed_ids]
```

### Task 1.3 — Construct the STORM modules in `__init__`

- [ ] Extend `AcademicWritingSkill.__init__` in `academic_writing_skill.py` to build the
  three STORM modules. Add these lines after the existing module construction
  (after `self._tst_module = StyleTransferModule()`).

```python
        self._perspective_module = PerspectiveGenerator()
        self._interview_module = ExpertInterviewer()
        self._synthesis_module = ResearchNotesSynthesizer()
```

### Task 1.4 — Add the `research_topic()` method

- [ ] Append `research_topic` to the `AcademicWritingSkill` class in
  `academic_writing_skill.py`. It runs the full STORM pre-writing loop and returns
  `ResearchNotes` whose `key_findings` feed `outline_skill`.

```python
    def research_topic(self, topic: str, refs: list[Ref],
                       n_perspectives: int = 3) -> ResearchNotes:
        """STORM-style perspective-guided pre-writing research phase.

        Generates `n_perspectives` diverse expert perspectives, interviews each one's
        questions against the supplied references (grounded answers only), then
        synthesizes the exchanges into structured ResearchNotes. The resulting
        `key_findings` are intended to seed `outline_skill` (Section.key_points).

        Raises ValueError if perspective generation yields no usable perspectives.
        """
        ref_dicts = [{"id": r.id, "title": r.title, "abstract": r.abstract} for r in refs]
        allowed_ids = {r.id for r in refs}

        # Stage 1: generate diverse expert perspectives.
        p_result = self._perspective_module(topic=topic, n_perspectives=n_perspectives)
        perspectives = _parse_perspectives_json(p_result.perspectives)
        if not perspectives:
            raise ValueError("research_topic: perspective generation returned no perspectives")

        # Stage 2: interview every question across all perspectives, grounded in refs.
        exchanges: list[InterviewExchange] = []
        for perspective in perspectives:
            for question in perspective.questions:
                i_result = self._interview_module(question=question, references=ref_dicts)
                answer = i_result.answer
                exchanges.append(
                    InterviewExchange(
                        question=question,
                        answer=answer,
                        cited_refs=_extract_cited_refs(answer, allowed_ids),
                    )
                )

        # Stage 3: synthesize the exchanges into structured notes.
        s_input = [{"question": e.question, "answer": e.answer} for e in exchanges]
        s_result = self._synthesis_module(topic=topic, exchanges=s_input)
        key_findings, suggested_sections = _parse_notes_json(s_result.notes)

        return ResearchNotes(
            topic=topic,
            perspectives=perspectives,
            exchanges=exchanges,
            key_findings=key_findings,
            suggested_sections=suggested_sections,
        )
```

---

## Phase 2 — Documentation (`SKILL.md`)

### Task 2.1 — Document `research_topic` in SKILL.md

- [ ] Add a pre-writing entry to the Pipeline section of
  `services/skills/academic-writing/SKILL.md`, before the existing `outline_skill` entry.

```markdown
0. `research_topic(topic, refs, n_perspectives=3)` — STORM pre-writing phase.
   Generates diverse expert perspectives, interviews each against the supplied
   references (grounded answers only), and synthesizes structured `ResearchNotes`.
   `ResearchNotes.key_findings` feed directly into `outline_skill` as section key_points.
```

### Task 2.2 — Add a STORM usage example to SKILL.md

- [ ] Add a usage snippet to the Usage section of `SKILL.md` showing the pre-writing phase
  feeding the outline.

````markdown
```python
# Pre-writing STORM research phase, then outline seeded by its findings.
notes = skill.research_topic(topic, refs, n_perspectives=3)
outline = skill.outline_skill(topic, refs)
for section in outline.sections:
    section.key_points.extend(notes.key_findings)
```
````

---

## Phase 3 — Tests (`test_storm.py`)

### Task 3.1 — Create `test_storm.py` with the prediction stub and fixtures

- [ ] Create `tests/services/skills/academic-writing/test_storm.py`. It reuses the existing
  `conftest.py` (which inserts `SKILL_DIR` on `sys.path` and provides `make_ref`). All tests
  monkeypatch the DSPy-backed modules so no network or real LLM call occurs.

```python
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
```

### Task 3.2 — Test `research_topic` returns n_perspectives perspectives

- [ ] Append to `test_storm.py`.

```python
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
```

### Task 3.3 — Test every Perspective has a non-empty questions list

- [ ] Append to `test_storm.py`.

```python
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
```

### Task 3.4 — Test `key_findings` is non-empty

- [ ] Append to `test_storm.py`.

```python
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
```

### Task 3.5 — Test interview answers only cite supplied ref ids

- [ ] Append to `test_storm.py`. The stubbed answer cites a real ref id and a ghost id;
  only the supplied id must survive into `cited_refs`.

```python
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
```

### Task 3.6 — Test `key_findings` feed `outline_skill` without error

- [ ] Append to `test_storm.py`. Stub the outline module too, then push the STORM findings
  into the resulting sections' `key_points` to confirm the contract holds end-to-end.

```python
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
```

### Task 3.7 — Test perspective parsing rejects non-JSON output

- [ ] Append to `test_storm.py`. Guards the `_parse_perspectives_json` error path.

```python
def test_research_topic_raises_on_unparseable_perspectives(monkeypatch, make_ref):
    skill = _storm_skill(
        monkeypatch,
        perspectives_json="I could not produce JSON, sorry.",
        answer_text="x",
        notes_json=_NOTES,
    )
    with pytest.raises(ValueError, match="no JSON array"):
        skill.research_topic("a topic", [make_ref("r1")])
```

### Task 3.8 — Run the STORM test suite

- [ ] Run only the new tests and confirm they pass with no network/LLM access.

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skills/academic-writing/test_storm.py -q
```

- [ ] Run the full academic-writing suite to confirm the extension did not regress the
  existing skill.

```bash
cd /Users/zachstallbohm/Work/gemma
python -m pytest tests/services/skills/academic-writing/ -q
```

---

## Phase 4 — Self-review

### Task 4.1 — Verify the STORM contract maps to the outline contract

- [ ] Confirm `ResearchNotes.key_findings` (a `list[str]`) is shape-compatible with
  `Section.key_points` (a `list[str]`), and that `research_topic` is documented as the
  pre-writing stage feeding `outline_skill`.

```bash
cd /Users/zachstallbohm/Work/gemma
grep -n "key_findings\|key_points" services/skills/academic-writing/academic_writing_skill.py
```

### Task 4.2 — Check critical-rule compliance

- [ ] Confirm no `tiktoken`, no `print(` in the skill server modules, and that all LLM calls
  go through DSPy modules (no direct `self._lm(...)` calls in `research_topic`).

```bash
cd /Users/zachstallbohm/Work/gemma
grep -rn "tiktoken" services/skills/academic-writing/ && echo "VIOLATION" || echo "clean: no tiktoken"
grep -rn "^\s*print(" services/skills/academic-writing/storm_modules.py && echo "VIOLATION" || echo "clean: no print"
```

### Task 4.3 — Check for placeholder patterns

- [ ] Grep the new files for unfilled placeholders.

```bash
cd /Users/zachstallbohm/Work/gemma
grep -rnE "TODO|FIXME|add appropriate|pass  # implement|\.\.\." \
  services/skills/academic-writing/storm_modules.py \
  tests/services/skills/academic-writing/test_storm.py || echo "no placeholders"
```

### Task 4.4 — Confirm no new dependencies were introduced

- [ ] `storm_modules.py` imports only `dataclasses` and `dspy`; `research_topic` adds no
  imports beyond `json`/`re` already used by the skill. Confirm `requirements.txt` is unchanged.

```bash
cd /Users/zachstallbohm/Work/gemma
grep -nE "^import|^from" services/skills/academic-writing/storm_modules.py
git diff --stat services/skills/academic-writing/requirements.txt 2>/dev/null || true
```
