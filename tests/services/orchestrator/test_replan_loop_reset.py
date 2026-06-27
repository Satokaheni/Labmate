from __future__ import annotations

import services.orchestrator.coding_orchestrator as co


def test_replan_max_skill_repeats_default_is_two():
    assert co.REPLAN_MAX_SKILL_REPEATS == 2
