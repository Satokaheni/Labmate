import importlib


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://example:6379/2")
    monkeypatch.setenv("JWT_SECRET", "topsecret")
    monkeypatch.setenv("ADMIN_EMAIL", "admin@labmate.local")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "$argon2id$fakehash")
    monkeypatch.setenv("JWT_EXPIRY_SECONDS", "3600")

    import services.ws_gateway.config as cfg
    importlib.reload(cfg)
    c = cfg.Config.from_env()
    assert c.redis_url == "redis://example:6379/2"
    assert c.jwt_secret == "topsecret"
    assert c.admin_email == "admin@labmate.local"
    assert c.admin_password_hash == "$argon2id$fakehash"
    assert c.jwt_expiry_seconds == 3600


def test_config_defaults(monkeypatch):
    for var in ("REDIS_URL", "JWT_EXPIRY_SECONDS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JWT_SECRET", "x")
    monkeypatch.setenv("ADMIN_EMAIL", "a@b.c")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "$argon2id$x")
    import services.ws_gateway.config as cfg
    importlib.reload(cfg)
    c = cfg.Config.from_env()
    assert c.redis_url == "redis://localhost:6379/0"
    assert c.jwt_expiry_seconds == 86400
