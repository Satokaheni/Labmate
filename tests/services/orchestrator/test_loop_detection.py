# tests/services/orchestrator/test_loop_detection.py
from __future__ import annotations

import pytest

from services.orchestrator.loop_detection import (
    LoopDetector,
    call_signature,
    LOOP_REPEAT_LIMIT,
    MUTATING_TOOLS,
    LOOP_REPEAT_LIMIT_MUTATING,
    repeat_limit_for,
)


@pytest.mark.mocked
class TestCallSignature:
    def test_signature_is_deterministic_regardless_of_key_order(self):
        a = call_signature("call_skill_tool", {"skill": "x", "tool": "y"})
        b = call_signature("call_skill_tool", {"tool": "y", "skill": "x"})
        assert a == b

    def test_signature_includes_tool_name(self):
        sig = call_signature("run_bash", {"command": "ls"})
        assert sig.startswith("run_bash")

    def test_different_args_produce_different_signatures(self):
        a = call_signature("read_file", {"path": "a.txt"})
        b = call_signature("read_file", {"path": "b.txt"})
        assert a != b

    def test_non_serializable_args_do_not_raise(self):
        # default=str must keep this from blowing up
        sig = call_signature("run_bash", {"obj": object()})
        assert sig.startswith("run_bash")


@pytest.mark.mocked
class TestLoopDetectorRepeat:
    def test_single_call_does_not_break(self):
        d = LoopDetector(repeat_limit=2)
        assert d.record(call_signature("run_bash", {"command": "ls"})) is False
        assert d.should_break() is False

    def test_consecutive_repeat_trips_at_limit(self):
        d = LoopDetector(repeat_limit=2)
        d.record(call_signature("run_bash", {"command": "ls"}))
        tripped = d.record(call_signature("run_bash", {"command": "ls"}))
        assert tripped is True
        assert d.should_break() is True
        assert d.reason() == "repeat"

    def test_distinct_calls_never_trip(self):
        d = LoopDetector(repeat_limit=2)
        d.record(call_signature("read_file", {"path": "a.txt"}))
        d.record(call_signature("read_file", {"path": "b.txt"}))
        assert d.record(call_signature("read_file", {"path": "c.txt"})) is False
        assert d.should_break() is False

    def test_repeat_limit_three_needs_three(self):
        d = LoopDetector(repeat_limit=3)
        d.record(call_signature("run_bash", {"command": "ls"}))
        assert d.record(call_signature("run_bash", {"command": "ls"})) is False
        assert d.record(call_signature("run_bash", {"command": "ls"})) is True
        assert d.reason() == "repeat"

    def test_default_limit_from_env_constant(self):
        # LOOP_REPEAT_LIMIT defaults to 2 unless overridden in the environment.
        assert LOOP_REPEAT_LIMIT >= 1
        d = LoopDetector()  # uses the module default
        d.record(call_signature("run_bash", {"command": "ls"}))
        # With default 2 a single repeat trips; tolerate higher env overrides.
        for _ in range(LOOP_REPEAT_LIMIT - 1):
            d.record(call_signature("run_bash", {"command": "ls"}))
        assert d.should_break() is True


@pytest.mark.mocked
class TestLoopDetectorCycle:
    def test_two_signature_cycle_trips(self):
        d = LoopDetector(repeat_limit=2)
        for cmd in ["ls", "pwd", "ls", "pwd"]:
            tripped = d.record(call_signature("run_bash", {"command": cmd}))
        assert tripped is True
        assert d.should_break() is True
        assert d.reason() == "cycle"

    def test_new_signature_resets_progress(self):
        d = LoopDetector(repeat_limit=2)
        d.record(call_signature("run_bash", {"command": "ls"}))
        d.record(call_signature("run_bash", {"command": "pwd"}))
        d.record(call_signature("run_bash", {"command": "ls"}))
        # A genuinely new command means progress — must NOT trip.
        assert d.record(call_signature("run_bash", {"command": "whoami"})) is False
        assert d.should_break() is False

    def test_reset_clears_history(self):
        d = LoopDetector(repeat_limit=2)
        d.record(call_signature("run_bash", {"command": "ls"}))
        d.record(call_signature("run_bash", {"command": "ls"}))
        assert d.should_break() is True
        d.reset()
        assert d.should_break() is False
        assert d.reason() == ""
        assert d.record(call_signature("run_bash", {"command": "ls"})) is False


@pytest.mark.mocked
class TestMutatingTolerance:
    def test_mutating_tools_membership(self):
        assert "write_file" in MUTATING_TOOLS
        assert "call_skill_tool" in MUTATING_TOOLS
        assert "read_file" not in MUTATING_TOOLS
        assert "run_tests" not in MUTATING_TOOLS

    def test_mutating_limit_higher_than_default(self):
        assert LOOP_REPEAT_LIMIT_MUTATING >= 4
        assert LOOP_REPEAT_LIMIT_MUTATING > LOOP_REPEAT_LIMIT

    def test_repeat_limit_for_mutating_returns_higher(self):
        assert repeat_limit_for("write_file") == LOOP_REPEAT_LIMIT_MUTATING
        assert repeat_limit_for("call_skill_tool") == LOOP_REPEAT_LIMIT_MUTATING

    def test_repeat_limit_for_read_returns_base(self):
        assert repeat_limit_for("read_file") == LOOP_REPEAT_LIMIT

    def test_per_call_override_tolerates_mutating_repeat(self):
        # Two identical write_file calls must NOT trip when the per-call
        # override raises the threshold to the mutating limit (>=4).
        d = LoopDetector(repeat_limit=2)
        sig = call_signature("write_file", {"path": "a.py", "content": "x"})
        assert d.record(sig, repeat_limit=LOOP_REPEAT_LIMIT_MUTATING) is False
        assert d.record(sig, repeat_limit=LOOP_REPEAT_LIMIT_MUTATING) is False
        assert d.should_break(repeat_limit=LOOP_REPEAT_LIMIT_MUTATING) is False

    def test_per_call_override_still_trips_at_mutating_limit(self):
        d = LoopDetector(repeat_limit=2)
        sig = call_signature("write_file", {"path": "a.py", "content": "x"})
        tripped = False
        for _ in range(LOOP_REPEAT_LIMIT_MUTATING):
            tripped = d.record(sig, repeat_limit=LOOP_REPEAT_LIMIT_MUTATING)
        assert tripped is True
        assert d.reason() == "repeat"

    def test_read_tool_thrash_still_trips_at_base_limit(self):
        # No override (or base override) -> default-2 behavior is unchanged.
        d = LoopDetector(repeat_limit=2)
        sig = call_signature("read_file", {"path": "a.py"})
        d.record(sig, repeat_limit=repeat_limit_for("read_file"))
        assert d.record(sig, repeat_limit=repeat_limit_for("read_file")) is True
        assert d.reason() == "repeat"

    def test_record_remains_callable_with_one_arg(self):
        # Backward-compat: existing call sites pass only the signature.
        d = LoopDetector(repeat_limit=2)
        sig = call_signature("run_bash", {"command": "ls"})
        d.record(sig)
        assert d.record(sig) is True
