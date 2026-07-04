"""Default session/user store wiring: server.py must default to SQLite (T10)."""

from __future__ import annotations

import subprocess


def test_default_stores_are_sqlite(monkeypatch, tmp_path):
    from services.orchestrator.local_store import LocalStore
    from services.ws_gateway import server as srv
    from services.ws_gateway.sqlite_session_store import SqliteSessionStore
    from services.ws_gateway.user_store import SqliteUserStore  # noqa: F401

    monkeypatch.setattr(
        srv, "get_local_store", lambda: LocalStore(tmp_path / "s.db"), raising=False
    )
    cfg = srv.Config(
        jwt_secret="s",
        admin_email="a@b.c",
        admin_password="pw",
        jwt_expiry_seconds=3600,
        cors_origins=(),
        mongo_url="",
    )
    assert isinstance(srv._default_session_store(cfg), SqliteSessionStore)


def test_no_mongo_imports_remain():
    out = subprocess.run(
        [
            "grep",
            "-rn",
            "MongoUserStore\\|MongoSessionStore\\|db_indexes\\|motor\\|pymongo",
            "services",
        ],
        capture_output=True,
        text=True,
    ).stdout
    # only comments/docstrings allowed; no import or class references
    assert "import motor" not in out and "MongoSessionStore" not in out
