import time
import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.ws_gateway.config import Config
from services.ws_gateway.auth import AuthService, build_auth_router


@pytest.fixture
def auth_service():
    ph = PasswordHasher()
    cfg = Config(
        redis_url="redis://localhost:6379/0",
        jwt_secret="test-secret",
        admin_email="admin@labmate.local",
        admin_password_hash=ph.hash("correct-horse"),
        jwt_expiry_seconds=3600,
        cors_origins=(),
    )
    return AuthService(cfg)


@pytest.fixture
def client(auth_service):
    app = FastAPI()
    app.include_router(build_auth_router(auth_service))
    return TestClient(app)


def test_valid_login_returns_token(client):
    r = client.post("/auth/login", json={"email": "admin@labmate.local", "password": "correct-horse"})
    assert r.status_code == 200
    body = r.json()
    assert body["token"]
    assert body["user"]["email"] == "admin@labmate.local"


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
    r = client.post("/auth/login", json={"email": "admin@labmate.local", "password": "correct-horse"})
    assert r.status_code == 423
    assert r.json()["detail"] == "locked"


def test_me_with_valid_token(client, auth_service):
    token = auth_service.mint_token()
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["email"] == "admin@labmate.local"


def test_me_with_expired_token_returns_401(client, auth_service):
    token = auth_service.mint_token(now=time.time() - 10_000, ttl=1)
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_verify_token_helper_round_trips(auth_service):
    token = auth_service.mint_token()
    user = auth_service.verify_token(token)
    assert user["email"] == "admin@labmate.local"


def test_verify_token_rejects_garbage(auth_service):
    assert auth_service.verify_token("not.a.jwt") is None
