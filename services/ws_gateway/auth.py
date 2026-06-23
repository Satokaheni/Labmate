from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from services.ws_gateway.config import Config

MAX_FAILURES = 5
LOCKOUT_SECONDS = 300


class LoginBody(BaseModel):
    email: str
    password: str


@dataclass
class _Attempts:
    count: int = 0
    locked_until: float = 0.0


class AuthService:
    """Stateless-JWT auth against a single argon2id-hashed admin credential."""

    def __init__(self, config: Config) -> None:
        self._cfg = config
        self._ph = PasswordHasher()
        self._user_id = "u-" + uuid.uuid5(uuid.NAMESPACE_DNS, config.admin_email).hex[:12]
        self._attempts: dict[str, _Attempts] = {}

    def user_record(self) -> dict:
        return {
            "id": self._user_id,
            "email": self._cfg.admin_email,
            "displayName": self._cfg.admin_email.split("@")[0],
            "createdAt": "2026-01-01T00:00:00Z",
        }

    def is_locked(self, email: str) -> bool:
        a = self._attempts.get(email)
        return bool(a and a.locked_until > time.time())

    def login(self, email: str, password: str) -> str:
        if self.is_locked(email):
            raise HTTPException(status_code=423, detail="locked")

        if email != self._cfg.admin_email or not self._verify_password(password):
            self._record_failure(email)
            raise HTTPException(status_code=401, detail="invalid_credentials")

        self._attempts.pop(email, None)
        return self.mint_token()

    def mint_token(self, now: float | None = None, ttl: int | None = None) -> str:
        issued = now if now is not None else time.time()
        expiry = ttl if ttl is not None else self._cfg.jwt_expiry_seconds
        payload = {
            "sub": self._user_id,
            "email": self._cfg.admin_email,
            "iat": int(issued),
            "exp": int(issued + expiry),
        }
        return jwt.encode(payload, self._cfg.jwt_secret, algorithm="HS256")

    def verify_token(self, token: str) -> dict | None:
        try:
            jwt.decode(token, self._cfg.jwt_secret, algorithms=["HS256"])
        except jwt.PyJWTError:
            return None
        return self.user_record()

    def _verify_password(self, password: str) -> bool:
        if not self._cfg.admin_password_hash:
            return False
        try:
            return self._ph.verify(self._cfg.admin_password_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            return False

    def _record_failure(self, email: str) -> None:
        a = self._attempts.setdefault(email, _Attempts())
        a.count += 1
        if a.count >= MAX_FAILURES:
            a.locked_until = time.time() + LOCKOUT_SECONDS


def build_auth_router(service: AuthService) -> APIRouter:
    router = APIRouter()

    @router.post("/auth/login")
    def login(body: LoginBody) -> dict:
        token = service.login(body.email, body.password)
        return {"token": token, "user": service.user_record()}

    @router.post("/auth/logout")
    def logout() -> dict:
        return {"ok": True}

    @router.get("/auth/me")
    def me(authorization: str = Header(default="")) -> dict:
        token = authorization.removeprefix("Bearer ").strip()
        user = service.verify_token(token)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid_token")
        return user

    return router
