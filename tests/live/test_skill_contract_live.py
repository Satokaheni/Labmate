import pytest

from tests.live.conftest import require_service
from tests.live.skill_harness import (
    runnable_manifests,
    declared_tools,
    register_skill,
    teardown_skill,
    SkillRegisterError,
)

pytestmark = pytest.mark.live

MANIFESTS = runnable_manifests()


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest", MANIFESTS, ids=[m.name for m in MANIFESTS])
async def test_skill_contract(manifest):
    try:
        reg, sp = await register_skill(manifest)
    except SkillRegisterError as exc:
        require_service(lambda: False, f"{manifest.name} register ({exc})")
        return
    try:
        # 1. advertises at least one tool
        assert sp.tools, f"{manifest.name} advertises no tools"
        # 2. every advertised tool has a JSON-Schema object input
        for tname, schema in sp.tools.items():
            assert isinstance(schema, dict), f"{manifest.name}.{tname} schema not a dict"
            assert schema.get("type") == "object", f"{manifest.name}.{tname} schema not an object"
        # 3. every SKILL.md-declared tool is actually advertised (doc<->server drift)
        missing = declared_tools(manifest.name) - set(sp.tools)
        assert not missing, f"{manifest.name}: SKILL.md declares tools not served: {missing}"
    finally:
        await teardown_skill(reg, sp)
