import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.ws_gateway.auth import AuthService, build_auth_router
from services.ws_gateway.config import Config


@pytest.fixture
async def auth_service(seeded_store):
    cfg = Config(
        jwt_secret="test-secret",
        admin_email="admin@labmate.local",
        admin_password="correct-horse",
        jwt_expiry_seconds=3600,
        cors_origins=(),
    )
    return AuthService(cfg, seeded_store)


@pytest.fixture
async def client(auth_service):
    app = FastAPI()
    app.include_router(build_auth_router(auth_service))
    return TestClient(app)


def test_valid_login_returns_token(client):
    r = client.post(
        "/auth/login", json={"email": "admin@labmate.local", "password": "correct-horse"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["token"]
    assert body["user"]["email"] == "admin@labmate.local"
    assert body["user"]["role"] == "admin"


def test_invalid_password_returns_401(client):
    r = client.post("/auth/login", json={"email": "admin@labmate.local", "password": "wrong"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid_credentials"


def test_unknown_email_returns_401(client):
    r = client.post("/auth/login", json={"email": "nobody@x.com", "password": "correct-horse"})
    assert r.status_code == 401


def test_lockout_after_five_failures(client):
    for _ in range(5):
        client.post("/auth/login", json={"email": "admin@labmate.local", "password": "wrong"})
    # 6th attempt, even with correct password, is locked
    r = client.post(
        "/auth/login", json={"email": "admin@labmate.local", "password": "correct-horse"}
    )
    assert r.status_code == 423
    assert r.json()["detail"] == "locked"


def test_me_with_valid_token(client, auth_service):
    token = auth_service.mint_token(
        {"id": "u-001", "email": "admin@labmate.local", "role": "admin"}
    )
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["email"] == "admin@labmate.local"


def test_me_with_expired_token_returns_401(client, auth_service):
    token = auth_service.mint_token(
        {"id": "u-001", "email": "admin@labmate.local", "role": "admin"},
        now=time.time() - 10_000,
        ttl=1,
    )
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_verify_token_helper_round_trips(auth_service):
    token = auth_service.mint_token(
        {"id": "u-001", "email": "admin@labmate.local", "role": "admin"}
    )
    claims = auth_service.verify_token(token)
    assert claims["email"] == "admin@labmate.local"
    assert claims["role"] == "admin"


def test_verify_token_rejects_garbage(auth_service):
    assert auth_service.verify_token("not.a.jwt") is None


def test_admin_can_create_user(client, auth_service):
    admin_token = auth_service.mint_token(
        {"id": "u-001", "email": "admin@labmate.local", "role": "admin"}
    )
    r = client.post(
        "/auth/users",
        json={"email": "newuser@example.com", "password": "pw123", "displayName": "New User"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 201
    assert r.json()["email"] == "newuser@example.com"


def test_non_admin_cannot_create_user(client, auth_service):
    user_token = auth_service.mint_token(
        {"id": "u-002", "email": "regular@example.com", "role": "user"}
    )
    r = client.post(
        "/auth/users",
        json={"email": "another@example.com", "password": "pw123", "displayName": "Another"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "admin_required"


def test_duplicate_email_returns_409(client, auth_service):
    admin_token = auth_service.mint_token(
        {"id": "u-001", "email": "admin@labmate.local", "role": "admin"}
    )
    r = client.post(
        "/auth/users",
        json={"email": "admin@labmate.local", "password": "pw123", "displayName": "Dupe"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 409
    assert r.json()["detail"] == "email_taken"


# ── single-user auto-auth (POST /auth/local) ─────────────────────────────────


def test_local_auth_returns_token_without_credentials(client):
    """Single-user mode (default): POST /auth/local mints a token for the seeded
    admin with NO credentials — the passwordless path for a local harness."""
    r = client.post("/auth/local", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["token"]
    assert body["user"]["email"] == "admin@labmate.local"
    assert body["user"]["role"] == "admin"


def test_local_auth_token_is_valid(client, auth_service):
    """The auto-auth token verifies and carries the admin identity."""
    token = client.post("/auth/local", json={}).json()["token"]
    claims = auth_service.verify_token(token)
    assert claims is not None
    assert claims["email"] == "admin@labmate.local"
    assert claims["role"] == "admin"


@pytest.fixture
async def multi_user_client(seeded_store):
    cfg = Config(
        jwt_secret="test-secret",
        admin_email="admin@labmate.local",
        admin_password="correct-horse",
        jwt_expiry_seconds=3600,
        cors_origins=(),
        single_user=False,
    )
    app = FastAPI()
    app.include_router(build_auth_router(AuthService(cfg, seeded_store)))
    return TestClient(app)


def test_local_auth_404_when_single_user_disabled(multi_user_client):
    """A multi-user gateway (single_user off) 404s /auth/local, so clients fall
    back to credential login."""
    r = multi_user_client.post("/auth/local", json={})
    assert r.status_code == 404


def test_config_single_user_defaults_on(monkeypatch):
    monkeypatch.delenv("LABMATE_SINGLE_USER", raising=False)
    assert Config.from_env().single_user is True


@pytest.mark.parametrize("val", ["0", "false", "False", ""])
def test_config_single_user_disabled_values(monkeypatch, val):
    monkeypatch.setenv("LABMATE_SINGLE_USER", val)
    assert Config.from_env().single_user is False
