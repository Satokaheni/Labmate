from __future__ import annotations
import csv
import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[4]
    / "services" / "skills" / "results-analysis" / "results_analysis.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("results_analysis", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def test_profile_results_detects_metric_columns(tmp_path):
    ra = _load()
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"id": "1", "config": "a", "accuracy": "0.80", "f1": "0.75"},
        {"id": "2", "config": "b", "accuracy": "0.90", "f1": "0.85"},
    ])
    out = ra.profile_results(str(csv_path))
    assert out["row_count"] == 2
    assert "accuracy" in out["columns"]
    assert "accuracy" in out["stats"]
    assert "mean" in out["stats"]["accuracy"]


def test_compare_runs_returns_pvalue_and_ci(tmp_path):
    ra = _load()
    csv_path = tmp_path / "runs.csv"
    rows = []
    for i in range(8):
        rows.append({"config": "baseline", "score": str(0.50 + i * 0.001)})
    for i in range(8):
        rows.append({"config": "treatment", "score": str(0.70 + i * 0.001)})
    _write_csv(csv_path, rows)
    out = ra.compare_runs(str(csv_path), group_col="config", metric_col="score")
    assert "p_value" in out
    assert isinstance(out["confidence_interval"], list)
    assert len(out["confidence_interval"]) == 2
    assert out["significant"] is True


def test_make_figures_writes_files_and_tables(tmp_path):
    ra = _load()
    csv_path = tmp_path / "results.csv"
    _write_csv(csv_path, [
        {"config": "a", "accuracy": "0.80"},
        {"config": "b", "accuracy": "0.90"},
    ])
    out_dir = tmp_path / "figs"
    out = ra.make_figures(str(csv_path), metric_cols=["accuracy"], output_dir=str(out_dir))
    assert out["figure_paths"]
    assert all(Path(p).exists() for p in out["figure_paths"])
    assert "accuracy" in out["markdown_table"]
    assert "accuracy" in out["latex_table"]


def test_skill_md_frontmatter_parses():
    import yaml, re
    skill_md = _MODULE_PATH.parent / "SKILL.md"
    assert skill_md.exists()
    text = skill_md.read_text(encoding="utf-8")
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
    assert m
    meta = yaml.safe_load(m.group(1))
    assert meta["name"] == "results-analysis"
    assert meta["requires"] == ["code-sandbox"]


def test_server_lists_three_tools():
    import importlib.util, asyncio
    server_path = _MODULE_PATH.parent / "server.py"
    spec = importlib.util.spec_from_file_location("results_analysis_server", server_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tools = asyncio.run(mod.list_tools())
    assert {t.name for t in tools} == {"profile_results", "compare_runs", "make_figures"}


def test_skill_runner_catalogs_results_analysis():
    from services.skill_runner.skill_runner import SkillRunner
    skills_root = _MODULE_PATH.resolve().parent.parent
    runner = SkillRunner(roots=[skills_root])
    runner.discover()
    assert "results-analysis" in runner.catalog
    prompt = runner.catalog_prompt()
    assert "results-analysis" in prompt
    assert "significance" in prompt.lower()
