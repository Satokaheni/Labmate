import importlib


def test_config_reads_env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "topsecret")
    monkeypatch.setenv("ADMIN_EMAIL", "admin@labmate.local")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-horse")
    monkeypatch.setenv("JWT_EXPIRY_SECONDS", "3600")
    monkeypatch.setenv("MONGO_URL", "mongodb://localhost:27017")

    import services.ws_gateway.config as cfg

    importlib.reload(cfg)
    c = cfg.Config.from_env()
    assert c.jwt_secret == "topsecret"
    assert c.admin_email == "admin@labmate.local"
    assert c.admin_password == "correct-horse"
    assert c.jwt_expiry_seconds == 3600
    assert c.mongo_url == "mongodb://localhost:27017"


def test_config_defaults(monkeypatch):
    for var in ("JWT_EXPIRY_SECONDS", "MONGO_URL", "ADMIN_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JWT_SECRET", "x")
    monkeypatch.setenv("ADMIN_EMAIL", "a@b.c")
    import services.ws_gateway.config as cfg

    importlib.reload(cfg)
    c = cfg.Config.from_env()
    assert c.jwt_expiry_seconds == 86400
    assert c.mongo_url == "mongodb://localhost:27017"
