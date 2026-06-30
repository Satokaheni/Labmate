import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.ws_gateway.auth import AuthService, build_auth_router
from services.ws_gateway.config import Config
from services.ws_gateway.user_store import InMemoryUserStore

pytestmark = pytest.mark.asyncio


def _cfg(**over):
    base = dict(
        redis_url="r",
        jwt_secret="test-secret",
        admin_email="admin@x.com",
        admin_password="pw",
        jwt_expiry_seconds=60,
        cors_origins=("*",),
        mongo_url="m",
    )
    base.update(over)
    return Config(**base)


async def _client_with_admin(cfg):
    store = InMemoryUserStore()
    auth = AuthService(cfg, store)
    admin = await auth.create_user("admin@x.com", "pw", "Admin", role="admin")
    token = auth.mint_token(admin)
    app = FastAPI()
    app.include_router(build_auth_router(auth))
    return TestClient(app), token


async def test_create_user_disabled_returns_403(self_unused=None):
    cfg = _cfg(enable_user_creation=False)
    client, token = await _client_with_admin(cfg)
    r = client.post(
        "/auth/users",
        json={"email": "new@x.com", "password": "pw2", "displayName": "New"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "user_creation_disabled"


async def test_create_user_enabled_admin_succeeds():
    cfg = _cfg(enable_user_creation=True)
    client, token = await _client_with_admin(cfg)
    r = client.post(
        "/auth/users",
        json={"email": "new@x.com", "password": "pw2", "displayName": "New"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201
    assert r.json()["email"] == "new@x.com"


async def test_create_user_enabled_non_admin_forbidden():
    cfg = _cfg(enable_user_creation=True)
    client, _admin_token = await _client_with_admin(cfg)
    r = client.post(
        "/auth/users",
        json={"email": "n@x.com", "password": "p", "displayName": "N"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "admin_required"
