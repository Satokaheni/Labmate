import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[4] / "services" / "skills" / "test-gen"
sys.path.insert(0, str(SKILL_DIR))


SAMPLE_MUTMUT_RUN = "🎉 18  🙁 4  ⏰ 0  🤔 0\nkilled: 18\nsurvived: 4\n"
SAMPLE_MUTMUT_RESULTS = "Survived 🙁 (2)\nsrc.calc.add_1\nsrc.calc.sub_2\n"
SAMPLE_DIFF_1 = "--- src/calc.py\n+++ src/calc.py\n@@ -1 +1 @@\n-    return a + b\n+    return a - b\n"


@pytest.fixture
def fake_runner():
    """Builds a CommandRunner whose responses are keyed by the mutmut subcommand."""
    from mutation_runner import CommandResult

    def make(run_out=SAMPLE_MUTMUT_RUN, run_code=1,
             results_out=SAMPLE_MUTMUT_RESULTS, show_out=SAMPLE_DIFF_1):
        def runner(argv, cwd, timeout):
            sub = argv[1] if len(argv) > 1 else ""
            if sub == "run":
                return CommandResult(run_code, run_out, "")
            if sub == "results":
                return CommandResult(0, results_out, "")
            if sub == "show":
                return CommandResult(0, show_out, "")
            return CommandResult(0, "", "")
        return runner

    return make
