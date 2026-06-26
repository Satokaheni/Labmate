from pathlib import Path

from services.skill_runner.skill_runner import SkillRunner

_FM = "---\nname: {name}\ndescription: {desc}\n---\nbody for {name}\n"


def _skill(root: Path, rel: str, name: str) -> None:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        _FM.format(name=name, desc=f"does {name}"), encoding="utf-8"
    )


def test_discover_skips_proposed(tmp_path: Path):
    root = tmp_path / "skills"
    _skill(root, "calc", "calc")                       # active
    _skill(root, ".proposed/review-fix", "review-fix")  # staged draft
    runner = SkillRunner(roots=[root])
    runner.discover()
    assert "calc" in runner.catalog
    assert "review-fix" not in runner.catalog
