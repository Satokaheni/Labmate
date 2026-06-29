from __future__ import annotations

import os
import secrets
from dataclasses import dataclass


def resolve_jwt_secret(env_secret: str | None, data_dir: str) -> str:
    """JWT secret resolution: explicit env wins; else load a persisted secret
    from <data_dir>/jwt_secret; else generate a strong one and persist it (0600).

    Persisting means tokens stay valid across restarts and the secret is never
    the insecure default."""
    if env_secret:
        return env_secret
    path = os.path.join(data_dir, "jwt_secret")
    try:
        with open(path, encoding="utf-8") as fh:
            existing = fh.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    secret = secrets.token_urlsafe(48)
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(secret)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return secret


@dataclass(frozen=True)
class Config:
    redis_url: str
    jwt_secret: str
    admin_email: str
    admin_password: str
    jwt_expiry_seconds: int
    cors_origins: tuple[str, ...]
    mongo_url: str

    @classmethod
    def from_env(cls) -> Config:
        origins = os.getenv("CORS_ORIGINS", "http://localhost:5173")
        return cls(
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            jwt_secret=os.getenv("JWT_SECRET", "dev-insecure-secret"),
            admin_email=os.getenv("ADMIN_EMAIL", "admin@labmate.local"),
            admin_password=os.getenv("ADMIN_PASSWORD", ""),
            jwt_expiry_seconds=int(os.getenv("JWT_EXPIRY_SECONDS", "86400")),
            cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
            mongo_url=os.getenv("MONGO_URL", "mongodb://localhost:27017"),
        )
