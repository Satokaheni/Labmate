from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    jwt_secret: str
    admin_email: str
    admin_password: str
    jwt_expiry_seconds: int
    cors_origins: tuple[str, ...]
    # Single-user-per-harness deployment (each user runs their OWN local, loopback
    # harness — like openclaw/hermes/ml-intern): a password is friction with no
    # security value. When on, the gateway exposes POST /auth/local to mint a token
    # for the seeded admin WITHOUT credentials. Default ON; set LABMATE_SINGLE_USER=0
    # for a shared/multi-user gateway (login required).
    single_user: bool = True

    @classmethod
    def from_env(cls) -> Config:
        origins = os.getenv("CORS_ORIGINS", "http://localhost:5173")
        return cls(
            jwt_secret=os.getenv("JWT_SECRET", "dev-insecure-secret"),
            admin_email=os.getenv("ADMIN_EMAIL", "admin@labmate.local"),
            admin_password=os.getenv("ADMIN_PASSWORD", ""),
            jwt_expiry_seconds=int(os.getenv("JWT_EXPIRY_SECONDS", "86400")),
            cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
            single_user=os.getenv("LABMATE_SINGLE_USER", "1") not in ("0", "false", "False", ""),
        )
