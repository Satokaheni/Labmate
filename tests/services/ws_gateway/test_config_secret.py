import os
import stat

from services.ws_gateway.config import resolve_jwt_secret


def test_env_secret_wins(tmp_path):
    assert resolve_jwt_secret("explicit", str(tmp_path)) == "explicit"
    assert not (tmp_path / "jwt_secret").exists()  # no file written when env given


def test_generates_and_persists_then_reuses(tmp_path):
    data_dir = str(tmp_path / "d")
    first = resolve_jwt_secret(None, data_dir)
    assert first and len(first) >= 32
    secret_file = tmp_path / "d" / "jwt_secret"
    assert secret_file.exists()
    assert stat.S_IMODE(os.stat(secret_file).st_mode) == 0o600
    # Second call reads the SAME secret back (stable across restarts).
    assert resolve_jwt_secret(None, data_dir) == first


def test_empty_env_treated_as_unset(tmp_path):
    data_dir = str(tmp_path / "d")
    s = resolve_jwt_secret("", data_dir)
    assert (tmp_path / "d" / "jwt_secret").exists()  # generated, not "" used
    assert s != ""
