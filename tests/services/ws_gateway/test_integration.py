import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from services.ws_gateway.config import Config
from services.ws_gateway.server import build_app


@pytest.fixture
def client(redis):
    ph = PasswordHasher()
    cfg = Config(
        redis_url="redis://localhost:6379/0",
        jwt_secret="s",
        admin_email="admin@labmate.local",
        admin_password_hash=ph.hash("pw"),
        jwt_expiry_seconds=3600,
        cors_origins=(),
    )

    async def ready(**_):
        return ("ready", "ok", "")

    checks = {k: ready for k in ("brain", "nervous_system", "hands", "memory", "workspace")}
    app = build_app(cfg, redis=redis, boot_checks=checks)
    return TestClient(app), app


def test_login_then_ws_auth_then_boot_ready(client):
    c, app = client
    # 1. REST login
    r = c.post("/auth/login", json={"email": "admin@labmate.local", "password": "pw"})
    assert r.status_code == 200
    token = r.json()["token"]

    # 2. WS connect + auth with the REST-minted token
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"type": "auth", "token": token})
        assert ws.receive_json()["type"] == "auth.ok"
        assert ws.receive_json()["type"] == "boot.plan"
        ev = ws.receive_json()
        while ev["type"] == "boot.update":
            ev = ws.receive_json()
        assert ev["type"] == "boot.ready"
        assert ev["sessionBootstrap"]["agentStatus"]["brain"]["state"] == "idle"


def test_healthz(client):
    c, _ = client
    assert c.get("/healthz").json() == {"ok": True}
