import logging
import sys
from pathlib import Path

import pytest

from services.skill_runner.skill_runner import SkillRunner, SkillMeta
import services.skill_runner.skill_runner as skill_runner


VALID_SKILL = """---
name: {name}
description: {desc}
---

# {name} body
This is the body of {name}.
"""


@pytest.mark.mocked
def test_discover_parses_frontmatter_only(write_skill, tmp_path):
    proj_root, _ = write_skill("project", "deploy", VALID_SKILL.format(name="deploy", desc="Deploys things."))
    pers_root, _ = write_skill("personal", "web-search", VALID_SKILL.format(name="web-search", desc="Searches the web."))

    runner = SkillRunner(roots=[proj_root, pers_root, tmp_path / "bundled"])
    runner.discover()

    assert set(runner.catalog) == {"deploy", "web-search"}
    deploy = runner.catalog["deploy"]
    assert isinstance(deploy, SkillMeta)
    assert deploy.name == "deploy"
    assert deploy.description == "Deploys things."
    assert deploy.tier == "project"
    assert deploy.path.name == "SKILL.md"
    # No body has been read into the activation cache.
    assert runner.loaded == {}


MISSING_DESC = """---
name: broken
---

# broken body
"""


@pytest.mark.mocked
def test_malformed_frontmatter_skipped_and_warns(write_skill, tmp_path, caplog):
    proj_root, bad_md = write_skill("project", "broken", MISSING_DESC)
    write_skill("project", "ok", VALID_SKILL.format(name="ok", desc="Fine skill."))

    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    with caplog.at_level(logging.WARNING, logger="skill_runner"):
        runner.discover()

    assert "broken" not in runner.catalog        # excluded
    assert "ok" in runner.catalog                # other skills still discovered
    # A warning naming the offending file path was logged.
    assert any(str(bad_md.resolve()) in rec.getMessage() for rec in caplog.records)


@pytest.mark.mocked
def test_project_tier_overrides_personal_on_collision(tmp_path, caplog):
    proj_root = tmp_path / "project"
    pers_root = tmp_path / "personal"
    (proj_root / "deploy").mkdir(parents=True)
    (pers_root / "deploy").mkdir(parents=True)
    proj_md = proj_root / "deploy" / "SKILL.md"
    pers_md = pers_root / "deploy" / "SKILL.md"
    proj_md.write_text(VALID_SKILL.format(name="deploy", desc="Project deploy."), encoding="utf-8")
    pers_md.write_text(VALID_SKILL.format(name="deploy", desc="Personal deploy."), encoding="utf-8")

    runner = SkillRunner(roots=[proj_root, pers_root, tmp_path / "bundled"])
    with caplog.at_level(logging.WARNING, logger="skill_runner"):
        runner.discover()

    entry = runner.catalog["deploy"]
    assert entry.path == proj_md.resolve()       # project wins
    assert entry.tier == "project"
    # Shadowing warning identifies the overridden personal path.
    assert any(str(pers_md.resolve()) in rec.getMessage() for rec in caplog.records)


MALICIOUS_YAML = """---
name: evil
description: !!python/object/apply:os.system ["echo pwned"]
---

# evil body
"""


@pytest.mark.mocked
def test_safe_loader_blocks_yaml_object_injection(write_skill, tmp_path):
    proj_root, _ = write_skill("project", "evil", MALICIOUS_YAML)

    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    # Must not execute os.system; either skipped (handled error) or plain data.
    runner.discover()

    # The malicious skill is not present as an executable object; if it parsed at
    # all, description is a string, never the result of os.system.
    if "evil" in runner.catalog:
        assert isinstance(runner.catalog["evil"].description, str)


@pytest.mark.mocked
def test_catalog_prompt_renders_sorted_compact_block(write_skill, tmp_path):
    proj_root, _ = write_skill("project", "deploy", VALID_SKILL.format(name="deploy", desc="Deploys things."))
    write_skill("project", "alpha", VALID_SKILL.format(name="alpha", desc="Alpha skill."))

    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    runner.discover()
    prompt = runner.catalog_prompt()

    lines = prompt.splitlines()
    assert lines[0] == "Available skills (call load_skill(name) to activate one):"
    assert lines[1] == "- alpha: Alpha skill."       # sorted by name
    assert lines[2] == "- deploy: Deploys things."


@pytest.mark.mocked
def test_tool_schema_exposes_load_skill_with_enum(write_skill, tmp_path):
    proj_root, _ = write_skill("project", "deploy", VALID_SKILL.format(name="deploy", desc="Deploys things."))
    write_skill("project", "alpha", VALID_SKILL.format(name="alpha", desc="Alpha skill."))

    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    runner.discover()
    schema = runner.tool_schema()

    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "load_skill"
    props = fn["parameters"]["properties"]
    assert props["name"]["enum"] == ["alpha", "deploy"]   # sorted
    assert fn["parameters"]["required"] == ["name"]


@pytest.mark.mocked
def test_load_skill_returns_body(write_skill, tmp_path):
    proj_root, _ = write_skill("project", "deploy", VALID_SKILL.format(name="deploy", desc="Deploys things."))
    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    runner.discover()

    result = runner.load_skill("deploy")
    assert result["name"] == "load_skill"
    assert result["response"]["status"] == "loaded"
    assert result["response"]["name"] == "deploy"
    assert "body of deploy" in result["response"]["body"]
    assert "deploy" in runner.loaded


@pytest.mark.mocked
def test_load_skill_dedup_returns_already_loaded(write_skill, tmp_path):
    proj_root, _ = write_skill("project", "deploy", VALID_SKILL.format(name="deploy", desc="Deploys things."))
    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    runner.discover()

    runner.load_skill("deploy")
    second = runner.load_skill("deploy")
    assert second["response"]["status"] == "already_loaded"
    assert "body" not in second["response"]      # not re-appended


@pytest.mark.mocked
def test_load_skill_unknown_returns_error_with_available(write_skill, tmp_path):
    proj_root, _ = write_skill("project", "deploy", VALID_SKILL.format(name="deploy", desc="Deploys things."))
    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    runner.discover()

    result = runner.load_skill("does-not-exist")
    assert result["response"]["status"] == "error"
    assert "unknown skill: does-not-exist" in result["response"]["message"]
    assert result["response"]["available"] == ["deploy"]


@pytest.mark.mocked
def test_chain_limit_blocks_further_activations(tmp_path):
    proj_root = tmp_path / "project"
    for n in ("a", "b", "c", "d"):
        d = proj_root / n
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            VALID_SKILL.format(name=n, desc=f"Skill {n}."), encoding="utf-8"
        )

    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"], max_chain=3)
    runner.discover()

    assert runner.load_skill("a")["response"]["status"] == "loaded"
    assert runner.load_skill("b")["response"]["status"] == "loaded"
    assert runner.load_skill("c")["response"]["status"] == "loaded"
    fourth = runner.load_skill("d")
    assert fourth["response"]["status"] == "error"
    assert "skill activation limit reached" in fourth["response"]["message"]
    assert "d" not in runner.loaded


@pytest.mark.mocked
def test_path_confinement_rejects_after_fs_tamper(write_skill, tmp_path, monkeypatch):
    proj_root, md = write_skill("project", "deploy", VALID_SKILL.format(name="deploy", desc="Deploys things."))
    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    runner.discover()

    # Simulate the catalog path being repointed outside all roots after discovery.
    outside = tmp_path / "outside" / "SKILL.md"
    outside.parent.mkdir(parents=True)
    outside.write_text(VALID_SKILL.format(name="deploy", desc="x"), encoding="utf-8")
    runner.catalog["deploy"] = skill_runner.SkillMeta(
        "deploy", "Deploys things.", outside.resolve(), "project", {}
    )

    result = runner.load_skill("deploy")
    assert result["response"]["status"] == "error"
    assert "path confinement violation" in result["response"]["message"]


@pytest.mark.mocked
def test_dispatch_rejects_unknown_tool_and_routes_load_skill(write_skill, tmp_path):
    proj_root, _ = write_skill("project", "deploy", VALID_SKILL.format(name="deploy", desc="Deploys things."))
    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    runner.discover()

    bad = runner.dispatch({"name": "not_load_skill", "arguments": {}})
    assert bad["response"]["status"] == "error"
    assert "unknown tool" in bad["response"]["message"]

    # arguments may arrive as a JSON string.
    ok = runner.dispatch({"name": "load_skill", "arguments": '{"name": "deploy"}'})
    assert ok["response"]["status"] == "loaded"


@pytest.mark.mocked
def test_package_exports():
    pkg_dir = Path(__file__).resolve().parents[3] / "services" / "skill_runner"
    init_path = pkg_dir / "__init__.py"
    assert init_path.exists()
    text = init_path.read_text(encoding="utf-8")
    for name in ("SkillRunner", "SkillRegistry", "SkillMeta", "SkillManifest", "SkillProcess"):
        assert name in text


@pytest.mark.mocked
def test_reload_catalog_rescans(write_skill, tmp_path):
    proj_root, _ = write_skill("project", "deploy", VALID_SKILL.format(name="deploy", desc="Deploys things."))
    runner = SkillRunner(roots=[proj_root, tmp_path / "personal", tmp_path / "bundled"])
    runner.discover()
    assert set(runner.catalog) == {"deploy"}

    # Add a new skill on disk, then re-scan.
    write_skill("project", "newskill", VALID_SKILL.format(name="newskill", desc="Brand new."))
    runner.reload_catalog()
    assert set(runner.catalog) == {"deploy", "newskill"}
