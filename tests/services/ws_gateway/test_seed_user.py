import asyncio

import pytest
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from services.orchestrator.local_store import LocalStore
from services.ws_gateway import seed_user


@pytest.fixture
def shared_store(tmp_path, monkeypatch):
    """A single LocalStore instance shared across CLI invocations in a test,
    so create + verify hit the same temp DB (mirrors get_local_store's
    process-wide singleton without touching the real global). `main()` owns
    its own event loop via asyncio.run, so these tests stay sync and use
    asyncio.run for store assertions too."""
    store = LocalStore(tmp_path / "state.db")
    monkeypatch.setattr(seed_user, "get_local_store", lambda: store)
    return store


def test_create_new_user(shared_store):
    rc = seed_user.main(["--email", "a@b.c", "--password", "pw", "--role", "admin"])
    assert rc == 0
    doc = asyncio.run(shared_store.auth_user_find_by_email("a@b.c"))
    assert doc is not None
    assert doc["role"] == "admin"
    PasswordHasher().verify(doc["passwordHash"], "pw")


def test_duplicate_email_without_reset_fails(shared_store):
    rc1 = seed_user.main(["--email", "a@b.c", "--password", "pw", "--role", "admin"])
    assert rc1 == 0
    before = asyncio.run(shared_store.auth_user_find_by_email("a@b.c"))

    rc2 = seed_user.main(["--email", "a@b.c", "--password", "other"])
    assert rc2 == 1

    after = asyncio.run(shared_store.auth_user_find_by_email("a@b.c"))
    assert after == before


def test_reset_password(shared_store):
    rc1 = seed_user.main(["--email", "a@b.c", "--password", "pw", "--role", "admin"])
    assert rc1 == 0

    rc2 = seed_user.main(["--email", "a@b.c", "--password", "newpw", "--reset-password"])
    assert rc2 == 0

    doc = asyncio.run(shared_store.auth_user_find_by_email("a@b.c"))
    assert doc is not None
    PasswordHasher().verify(doc["passwordHash"], "newpw")
    with pytest.raises(VerifyMismatchError):
        PasswordHasher().verify(doc["passwordHash"], "pw")
