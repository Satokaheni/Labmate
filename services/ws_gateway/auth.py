from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from services.ws_gateway.config import Config
from services.ws_gateway.user_store import UserDoc, UserStore

MAX_FAILURES = 5
LOCKOUT_SECONDS = 300


class LoginBody(BaseModel):
    email: str
    password: str


class CreateUserBody(BaseModel):
    email: str
    password: str
    displayName: str = ""


@dataclass
class _Attempts:
    count: int = 0
    locked_until: float = 0.0


class AuthService:
    """JWT auth backed by a UserStore. Supports multiple user accounts."""

    def __init__(self, config: Config, user_store: UserStore) -> None:
        self._cfg = config
        self._store = user_store
        self._ph = PasswordHasher()
        self._attempts: dict[str, _Attempts] = {}

    @property
    def user_creation_enabled(self) -> bool:
        return self._cfg.enable_user_creation

    def is_locked(self, email: str) -> bool:
        a = self._attempts.get(email)
        return bool(a and a.locked_until > time.time())

    async def login(self, email: str, password: str) -> str:
        if self.is_locked(email):
            raise HTTPException(status_code=423, detail="locked")

        user = await self._store.find_by_email(email)
        if user is None:
            self._record_failure(email)
            raise HTTPException(status_code=401, detail="invalid_credentials")

        try:
            ok = self._ph.verify(user["passwordHash"], password)
        except (VerifyMismatchError, InvalidHashError):
            ok = False

        if not ok:
            self._record_failure(email)
            raise HTTPException(status_code=401, detail="invalid_credentials")

        self._attempts.pop(email, None)
        return self.mint_token(user)

    async def create_user(
        self,
        email: str,
        password: str,
        display_name: str = "",
        role: Literal["admin", "user"] = "user",
    ) -> UserDoc:
        existing = await self._store.find_by_email(email)
        if existing is not None:
            raise HTTPException(status_code=409, detail="email_taken")
        pw_hash = self._ph.hash(password)
        return await self._store.create(
            email=email,
            display_name=display_name,
            password_hash=pw_hash,
            role=role,
        )

    def mint_token(self, user: dict, now: float | None = None, ttl: int | None = None) -> str:
        issued = now if now is not None else time.time()
        expiry = ttl if ttl is not None else self._cfg.jwt_expiry_seconds
        payload = {
            "sub": user["id"],
            "email": user["email"],
            "role": user.get("role", "user"),
            "iat": int(issued),
            "exp": int(issued + expiry),
        }
        return jwt.encode(payload, self._cfg.jwt_secret, algorithm="HS256")

    def verify_token(self, token: str) -> dict | None:
        try:
            return jwt.decode(token, self._cfg.jwt_secret, algorithms=["HS256"])
        except jwt.PyJWTError:
            return None

    def _record_failure(self, email: str) -> None:
        a = self._attempts.setdefault(email, _Attempts())
        a.count += 1
        if a.count >= MAX_FAILURES:
            a.locked_until = time.time() + LOCKOUT_SECONDS


def build_auth_router(service: AuthService) -> APIRouter:
    router = APIRouter()

    @router.post("/auth/login")
    async def login(body: LoginBody) -> dict:
        token = await service.login(body.email, body.password)
        claims = service.verify_token(token)
        return {
            "token": token,
            "user": {
                "id": claims["sub"],
                "email": claims["email"],
                "role": claims.get("role", "user"),
            },
        }

    @router.post("/auth/logout")
    def logout() -> dict:
        return {"ok": True}

    @router.get("/auth/me")
    def me(authorization: str = Header(default="")) -> dict:
        token = authorization.removeprefix("Bearer ").strip()
        claims = service.verify_token(token)
        if claims is None:
            raise HTTPException(status_code=401, detail="invalid_token")
        return {"id": claims["sub"], "email": claims["email"], "role": claims.get("role", "user")}

    @router.post("/auth/users", status_code=201)
    async def create_user(body: CreateUserBody, authorization: str = Header(default="")) -> dict:
        if not service.user_creation_enabled:
            raise HTTPException(status_code=403, detail="user_creation_disabled")
        token = authorization.removeprefix("Bearer ").strip()
        claims = service.verify_token(token)
        if not claims or claims.get("role") != "admin":
            raise HTTPException(status_code=403, detail="admin_required")
        user = await service.create_user(body.email, body.password, body.displayName, role="user")
        return {"id": user["id"], "email": user["email"]}

    return router
