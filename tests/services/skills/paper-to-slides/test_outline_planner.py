import pytest

from outline_planner import OutlinePlanner

pytestmark = pytest.mark.mocked


def _blueprint_obj(n):
    return {"paper_title": "On X", "authors": ["A. Author"], "venue": "NeurIPS",
            "slides": [{"index": i, "title": f"S{i}",
                        "section": ("methods" if i in (3, 4, 5) else "intro"),
                        "bullets": ["b"], "figure_paths": [], "table_html": None,
                        "speaker_note_hint": "h"} for i in range(1, n + 1)]}


def test_plan_returns_blueprint(monkeypatch, parsed_paper, llm_json):
    monkeypatch.setattr("outline_planner.litellm.completion",
                        lambda **k: llm_json(_blueprint_obj(13)))
    bp = OutlinePlanner().plan(parsed_paper, talk_duration_min=20)
    assert bp.target_slide_count == 10  # max(6, 20/2)
    assert len(bp.slides) == 13
    assert bp.talk_duration_min == 20


def test_methods_section_mapped(monkeypatch, parsed_paper, llm_json):
    monkeypatch.setattr("outline_planner.litellm.completion",
                        lambda **k: llm_json(_blueprint_obj(13)))
    bp = OutlinePlanner().plan(parsed_paper, talk_duration_min=20)
    assert any(s.section == "methods" for s in bp.slides)


def test_target_slide_count_rule():
    assert OutlinePlanner._target_slide_count(20) == 10
    assert OutlinePlanner._target_slide_count(4) == 6   # floor
