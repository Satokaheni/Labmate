"""Fixture-based unit tests for the pure engineering scorecard generator.

CI-safe: no GEMMA_BASE, no Redis, no model, no orchestrator internals — plain
stdlib file reads and string/regex counting. This is the decider part (ii) of
the LangGraph-removal spike (Task 8 parity gate + Task 9 fault scorer are the
other decision inputs); this module computes LOC delta, droppable deps, and
renders the ergonomics note into SCORECARD.md.
"""

from __future__ import annotations

from pathlib import Path

from eval.orchestrator_ab.scorecard import (
    build_scorecard,
    count_lines,
    droppable_deps,
    render_markdown,
    scaffold_loc,
)


class TestCountLines:
    def test_counts_lines_in_fixture_file(self, tmp_path: Path):
        f = tmp_path / "sample.py"
        f.write_text("line one\nline two\nline three\n")
        assert count_lines("sample.py", root=tmp_path) == 3

    def test_missing_file_returns_zero(self, tmp_path: Path):
        assert count_lines("does_not_exist.py", root=tmp_path) == 0


class TestScaffoldLoc:
    def test_counts_matching_scaffold_lines(self, tmp_path: Path):
        f = tmp_path / "graph.py"
        f.write_text(
            "\n".join(
                [
                    "from langgraph.graph import StateGraph",
                    "def build():",
                    "    builder = StateGraph(State)",
                    "    builder.add_node('a', a)",
                    "    builder.add_edge('a', 'b')",
                    "    return builder.compile()",
                    "",
                    "async def assess_ambiguity(state):",
                    "    return {}",
                ]
            )
        )
        assert scaffold_loc("graph.py", root=tmp_path) == 5

    def test_file_with_none_returns_zero(self, tmp_path: Path):
        f = tmp_path / "plain.py"
        f.write_text("def foo():\n    return 1\n")
        assert scaffold_loc("plain.py", root=tmp_path) == 0

    def test_missing_file_returns_zero(self, tmp_path: Path):
        assert scaffold_loc("nope.py", root=tmp_path) == 0


class TestDroppableDeps:
    def test_returns_only_langgraph_lines(self, tmp_path: Path):
        f = tmp_path / "requirements.txt"
        f.write_text(
            "\n".join(
                [
                    "langgraph>=0.2",
                    "langgraph-checkpoint-sqlite>=3.1",
                    "httpx>=0.27",
                ]
            )
        )
        deps = droppable_deps("requirements.txt", root=tmp_path)
        assert deps == ["langgraph>=0.2", "langgraph-checkpoint-sqlite>=3.1"]

    def test_missing_file_returns_empty_list(self, tmp_path: Path):
        assert droppable_deps("nope.txt", root=tmp_path) == []


class TestBuildScorecardOnRealRepo:
    def test_lite_loc_added_is_sane(self):
        data = build_scorecard()
        assert data["lite_loc_added"] > 300

    def test_graph_scaffold_removable_is_positive(self):
        data = build_scorecard()
        assert data["graph_scaffold_removable"] > 0

    def test_droppable_deps_contains_both_langgraph_lines(self):
        data = build_scorecard()
        deps = data["droppable_deps"]
        assert any(d.startswith("langgraph>=") for d in deps)
        assert any("langgraph-checkpoint-sqlite" in d for d in deps)


class TestRenderMarkdown:
    def test_contains_required_sections_and_honesty_note(self):
        data = build_scorecard()
        md = render_markdown(data)
        assert "# LangGraph-Removal Spike" in md
        assert "## LOC delta" in md
        assert "## Droppable dependencies" in md
        assert "## Ergonomics" in md
        assert "not dead weight" in md
        for d in data["droppable_deps"]:
            assert d in md


class TestNoOrchestratorDeps:
    def test_module_imports_with_zero_orchestrator_deps(self):
        import sys

        before = {m for m in sys.modules if m.startswith("services.orchestrator")}
        from eval.orchestrator_ab import scorecard  # noqa: F401

        after = {m for m in sys.modules if m.startswith("services.orchestrator")}
        assert after == before, f"orchestrator modules imported eagerly: {after - before}"
