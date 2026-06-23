from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    redis_url: str
    jwt_secret: str
    admin_email: str
    admin_password_hash: str
    jwt_expiry_seconds: int
    cors_origins: tuple[str, ...]

    @classmethod
    def from_env(cls) -> "Config":
        origins = os.getenv("CORS_ORIGINS", "http://localhost:5173")
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            jwt_secret=os.getenv("JWT_SECRET", "dev-insecure-secret"),
            admin_email=os.getenv("ADMIN_EMAIL", "admin@labmate.local"),
            admin_password_hash=os.getenv("ADMIN_PASSWORD_HASH", ""),
            jwt_expiry_seconds=int(os.getenv("JWT_EXPIRY_SECONDS", "86400")),
            cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
        )
