from services.orchestrator.prompt_assembler import PromptAssembler


def test_system_message_explains_oob_marker_is_real():
    sysmsg = PromptAssembler(skill_router=None, codegraph_enabled=False).system_message()
    text = sysmsg["content"] if isinstance(sysmsg, dict) else str(sysmsg)
    assert "OUT-OF-BAND USER MESSAGE" in text
    # The guidance must frame it as a genuine user instruction, not injection.
    assert "genuine" in text.lower() or "real user" in text.lower()
