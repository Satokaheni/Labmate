import pytest

from tests.live.conftest import require_service
from tests.live.skill_harness import (
    runnable_manifests, register_skill, teardown_skill, SkillRegisterError,
)
from services.skill_runner.skill_registry import SkillUnavailable

pytestmark = pytest.mark.live

MANIFESTS = runnable_manifests()


@pytest.mark.asyncio
@pytest.mark.parametrize("manifest", MANIFESTS, ids=[m.name for m in MANIFESTS])
async def test_unknown_tool_enumerates_valid_names(manifest):
    try:
        reg, sp = await register_skill(manifest)
    except SkillRegisterError as exc:
        require_service(lambda: False, f"{manifest.name} register ({exc})")
        return
    try:
        with pytest.raises(SkillUnavailable) as exc:
            await reg.call_tool(f"{manifest.name}.__definitely_not_a_tool__", {})
        msg = str(exc.value)
        assert "__definitely_not_a_tool__" in msg
        # at least one real tool name appears in the enumerated list
        assert any(t in msg for t in sp.tools), f"{manifest.name}: error did not enumerate valid tools"
    finally:
        await teardown_skill(reg, sp)
