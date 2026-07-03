from pathlib import Path

from eval.metric_meaning import (
    ROUTING_MEANING,
    routing_header_lines,
    seq_ab_meaning_block,
)
from eval.run_routing_eval import write_reports


def test_routing_meaning_names_the_proxy():
    text = ROUTING_MEANING.lower()
    assert "proxy" in text
    assert "skill selection" in text
    assert "not" in text and "task completion" in text


def test_routing_header_lines_are_markdown():
    lines = routing_header_lines()
    assert any(line.startswith("> ") for line in lines)  # blockquote caption
    assert any("proxy" in line.lower() for line in lines)


def test_seq_ab_meaning_flags_self_report():
    block = seq_ab_meaning_block()
    assert block["ok_metric"] == "proxy"
    assert "self-report" in block["note"].lower()


def test_routing_report_starts_with_meaning(tmp_path):
    summary = {
        "overall": 0.9,
        "n": 10,
        "mean_stability": 1.0,
        "by_cluster": {},
        "by_skill": {},
        "by_kind": {},
        "false_positive_rate": None,
        "confusion": [],
    }
    md = write_reports([], summary, str(tmp_path), repeats=3)
    body = Path(md).read_text().lower()
    assert "proxy" in body
    assert body.index("proxy") < body.index("overall accuracy")
