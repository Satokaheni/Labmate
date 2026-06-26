# tests/services/orchestrator/test_loop_detection.py
from __future__ import annotations

import pytest

from services.orchestrator.loop_detection import (
    LoopDetector,
    call_signature,
    LOOP_REPEAT_LIMIT,
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
