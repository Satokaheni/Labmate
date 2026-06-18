import pytest

import figure_triage
from figure_triage import FigureTriage

pytestmark = pytest.mark.mocked


def test_score_figure(monkeypatch):
    monkeypatch.setattr(figure_triage, "_encode_png", lambda p: "AAAA")
    monkeypatch.setattr(figure_triage.litellm, "completion", lambda **k: {
        "choices": [{"message": {"content":
            '{"slide_worthy": true, "score": 0.9, "description": "clear"}'}}]})
    fs = FigureTriage().score_figure("/tmp/fig1.png", "cap")
    assert fs.slide_worthy and fs.score == 0.9


def test_triage_survives_bad_figure(monkeypatch):
    def _boom(p): raise FileNotFoundError(p)
    monkeypatch.setattr(figure_triage, "_encode_png", _boom)
    out = FigureTriage().triage([{"path": "/tmp/missing.png", "caption": ""}])
    assert len(out) == 1 and out[0].slide_worthy is False
