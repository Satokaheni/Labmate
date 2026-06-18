import pytest

from outline_planner import PresentationBlueprint, SlideBlueprint
from slide_generator import SlideGenerator

pytestmark = pytest.mark.mocked


def _bp():
    return PresentationBlueprint(
        paper_title="On X & Y", authors=["A"], venue="NeurIPS",
        talk_duration_min=20, target_slide_count=2,
        slides=[
            SlideBlueprint(1, "Title", "title"),
            SlideBlueprint(2, "Methods", "methods", bullets=["uses 50% data"],
                           figure_paths=["/tmp/fig1.png"]),
        ])


def test_to_beamer_has_frames_and_escapes():
    tex = SlideGenerator().to_beamer(_bp())
    assert "\\begin{document}" in tex and "\\frame{\\titlepage}" in tex
    assert "\\begin{frame}{Methods}" in tex
    assert r"\&" in tex and r"\%" in tex          # escaping
    assert "/tmp/fig1.png" in tex


def test_to_marp_has_separators():
    md = SlideGenerator().to_marp(_bp())
    assert "marp: true" in md and "---" in md
    assert "## Methods" in md and "![w:800](/tmp/fig1.png)" in md


def test_generate_writes_file(tmp_path):
    p = SlideGenerator().generate(_bp(), str(tmp_path), "beamer")
    assert p.endswith("slides.tex")
