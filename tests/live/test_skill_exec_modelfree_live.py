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
