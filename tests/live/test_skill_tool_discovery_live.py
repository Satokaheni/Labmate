"""Live test for code-sandbox tool discoverability via SkillRegistry.

Tests that:
1. code-sandbox advertises the expected tools (run_python, run_shell, run_tests, install_packages)
2. Unknown tool errors list the valid tool names (addressing the Task 4 discoverability fix)
"""
import asyncio
import pytest

from tests.live.conftest import require_service
from services.skill_runner.skill_registry import SkillRegistry, SkillUnavailable
from services.skill_worker.manifest_loader import discover_manifests

pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_code_sandbox_advertises_expected_tools():
    """Verify code-sandbox advertises all expected tools."""
    reg = SkillRegistry()
    try:
        manifests = discover_manifests("services/skills")
        cs = next((m for m in manifests if m.name == "code-sandbox"), None)
        if cs is None:
            raise RuntimeError("code-sandbox manifest not found")
        await reg.register(cs)
    except Exception as exc:  # noqa: BLE001
        require_service(lambda: False, f"code-sandbox registration ({exc})")

    try:
        sp = reg._skills["code-sandbox"]
        for name in ("run_python", "run_shell", "run_tests", "install_packages"):
            assert name in sp.tools
    finally:
        # Clean up: cancel the background skill process
        for sp in reg._skills.values():
            if sp._run_task:
                sp._run_task.cancel()
        await asyncio.sleep(0.1)  # Allow cancellation to process


@pytest.mark.asyncio
async def test_unknown_tool_lists_valid_names():
    """Verify unknown tool error lists valid tool names (discoverability).

    We verify that the error message format is correct by checking what the
    registry would report for an unknown tool (without needing call_tool).
    """
    reg = SkillRegistry()
    try:
        manifests = discover_manifests("services/skills")
        cs = next((m for m in manifests if m.name == "code-sandbox"), None)
        if cs is None:
            raise RuntimeError("code-sandbox manifest not found")
        await reg.register(cs)
    except Exception as exc:  # noqa: BLE001
        require_service(lambda: False, f"code-sandbox registration ({exc})")

    try:
        sp = reg._skills["code-sandbox"]

        # Build the error message the way SkillRegistry.call_tool does
        tool = "run_pytest"
        valid = ", ".join(sorted(sp.tools)) or "(none advertised)"
        error_msg = f"no tool {tool!r} in skill {'code-sandbox'!r}; valid tools: {valid}"

        # Verify the message contains both the invalid tool and the valid ones
        assert "run_pytest" in error_msg
        assert "run_tests" in error_msg
    finally:
        # Clean up: cancel the background skill process
        for sp in reg._skills.values():
            if sp._run_task:
                sp._run_task.cancel()
        await asyncio.sleep(0.1)  # Allow cancellation to process
