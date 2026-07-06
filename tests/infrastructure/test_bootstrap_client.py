import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "infrastructure" / "bootstrap-client.sh"


def test_dry_run_lists_both_steps_in_order():
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    i_install = out.find("install.sh --client-only")
    i_build = out.find("npm run build:main")
    assert i_install != -1 and i_build != -1
    assert i_install < i_build  # install BEFORE the frontend build
    assert "launch the app" in out.lower()
