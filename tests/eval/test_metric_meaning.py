from eval.metric_meaning import (
    ROUTING_MEANING,
    routing_header_lines,
    seq_ab_meaning_block,
)


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
