from services.ws_gateway.config import Config


def test_from_env_defaults_sqlite_and_locked(monkeypatch, tmp_path):
    for var in ("USER_STORE", "ENABLE_USER_CREATION", "JWT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LABMATE_DATA_DIR", str(tmp_path))
    cfg = Config.from_env()
    assert cfg.user_store == "sqlite"
    assert cfg.enable_user_creation is False
    assert cfg.data_dir == str(tmp_path)
    # jwt secret was generated + persisted under data_dir (not the insecure default)
    assert cfg.jwt_secret != "dev-insecure-secret"
    assert (tmp_path / "jwt_secret").exists()


def test_from_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("LABMATE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("USER_STORE", "memory")
    monkeypatch.setenv("ENABLE_USER_CREATION", "1")
    monkeypatch.setenv("JWT_SECRET", "explicit-secret")
    cfg = Config.from_env()
    assert cfg.user_store == "memory"
    assert cfg.enable_user_creation is True
    assert cfg.jwt_secret == "explicit-secret"
    assert not (tmp_path / "jwt_secret").exists()  # env secret → no file
