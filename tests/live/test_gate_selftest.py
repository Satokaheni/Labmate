import os
import pytest
from tests.live.conftest import live_enabled

pytestmark = pytest.mark.live


def test_live_gate_matches_env():
    assert live_enabled() == (os.getenv("LIVE_TESTS") == "1")
