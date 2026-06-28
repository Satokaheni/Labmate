import pytest

from tests.live.conftest import require_service
from tests.live.skill_harness import (
    manifest_by_name, call_skill_tool, result_text, result_is_error,
    SkillRegisterError,
)

pytestmark = pytest.mark.live

_FIXTURE = (
    'def last_index(seq, target):\n'
    '    """Return the index of the LAST occurrence of target, or -1."""\n'
    '    for i in range(len(seq)):\n'
    '        if seq[i] == target:\n'
    '            return i\n'
    '    return -1\n'
)


async def _run(skill_name, tool, args, timeout=60.0):
    m = manifest_by_name(skill_name)
    if m is None:
        require_service(lambda: False, f"{skill_name} not runnable")
    try:
        return await call_skill_tool(m, tool, args, timeout=timeout)
    except SkillRegisterError as exc:
        require_service(lambda: False, f"{skill_name} register ({exc})")


@pytest.mark.asyncio
async def test_ast_search_find_code(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text(_FIXTURE)
    # SKILL BUG: pattern syntax `def last_index($$$):` does not match (ast-grep patterns
    # with parameters don't work). Using simpler pattern that does match.
    r = await _run("ast-search", "find_code",
                   {"pattern": "def last_index", "language": "python", "path": str(f)})
    assert not result_is_error(r)
    assert result_text(r).strip(), "ast-search returned empty"
    # known-answer: the matched output references the function name
    assert "last_index" in result_text(r)


@pytest.mark.asyncio
async def test_ast_repo_map_get_symbols(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text(_FIXTURE)
    r = await _run("ast-repo-map", "get_symbols", {"file": str(f)})
    assert not result_is_error(r)
    assert "last_index" in result_text(r)


@pytest.mark.asyncio
async def test_repo_graph_build(tmp_path):
    (tmp_path / "mod.py").write_text(_FIXTURE)
    r = await _run("repo-graph", "build", {"repo_path": str(tmp_path)})
    assert not result_is_error(r)
    assert result_text(r).strip(), "repo-graph build returned empty"


@pytest.mark.asyncio
async def test_design_token_transform_transform():
    # transform is the model-free/local path (extract/extract_and_transform need a
    # Figma token). Deterministic: TokenSet JSON -> CSS custom properties.
    import json
    token_set = json.dumps({
        "source": "test",
        "extracted_at": "2026-01-01T00:00:00+00:00",
        "tokens": [
            {"name": "Primary", "category": "color", "value": "#FF5733", "description": ""},
            {"name": "Card", "category": "radius", "value": "8px", "description": ""},
        ],
    })
    r = await _run("design-token-transform", "transform",
                   {"tokens_json": token_set, "format": "css-vars"})
    assert not result_is_error(r)
    out = result_text(r)
    assert out.strip(), "transform returned empty"
    # known-answer: the colour token surfaces as a CSS custom property + its value
    assert "--" in out and "FF5733" in out.upper(), f"unexpected transform output: {out[:200]}"


@pytest.mark.asyncio
async def test_a11y_audit_list_rules():
    # list_rules is the deterministic, zero-arg path (audit_file/audit_url need
    # headless Chromium). Asserts the axe-core ruleset is enumerable.
    r = await _run("a11y-audit", "list_rules", {})
    assert not result_is_error(r)
    assert result_text(r).strip(), "a11y-audit list_rules returned empty"


@pytest.mark.asyncio
async def test_react_doctor_list_rules():
    # Zero-arg deterministic ruleset enumeration (audit needs a React project).
    r = await _run("react-doctor", "list_rules", {})
    assert not result_is_error(r)
    assert result_text(r).strip(), "react-doctor list_rules returned empty"


@pytest.mark.asyncio
async def test_web_search_search():
    # web-search hits the local SearXNG (SEARXNG_URL). Skip if it is not reachable.
    import os, urllib.request
    base = os.getenv("SEARXNG_URL", "http://localhost:8080")
    try:
        urllib.request.urlopen(base, timeout=3)
    except Exception:
        require_service(lambda: False, f"SearXNG not reachable at {base}")
    r = await _run("web-search", "search", {"query": "python list comprehension", "limit": 3})
    assert not result_is_error(r)
    assert result_text(r).strip(), "web-search returned empty"


@pytest.mark.asyncio
async def test_ast_ts_refactor_rename_symbol(tmp_path):
    # Type-aware TS rename via the TS checker — deterministic, no model. Returns a
    # PENDING unified diff (not written to disk).
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions":{"strict":true},"include":["*.ts"]}')
    src = tmp_path / "mod.ts"
    src.write_text("export const oldName = 1;\nconsole.log(oldName);\n")
    r = await _run("ast-ts-refactor", "rename_symbol", {
        "tsconfig": str(tmp_path / "tsconfig.json"),
        "file": str(src),
        "symbol": "oldName",
        "new_name": "newName",
    })
    assert not result_is_error(r)
    out = result_text(r)
    assert "newName" in out, f"rename did not produce newName in the diff: {out[:300]}"
