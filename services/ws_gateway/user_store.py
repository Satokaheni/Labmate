from __future__ import annotations

import asyncio
import os
import sqlite3
import time
import uuid
from typing import Literal, Protocol, TypedDict


class UserDoc(TypedDict):
    id: str
    email: str
    displayName: str
    passwordHash: str
    role: Literal["admin", "user"]
    createdAt: str


class UserStore(Protocol):
    async def find_by_email(self, email: str) -> UserDoc | None: ...
    async def create(
        self,
        *,
        email: str,
        display_name: str,
        password_hash: str,
        role: Literal["admin", "user"] = "user",
    ) -> UserDoc: ...
    async def count(self) -> int: ...


class InMemoryUserStore:
    """Test-only in-memory implementation — no Motor dependency."""

    def __init__(self) -> None:
        self._users: dict[str, UserDoc] = {}

    async def find_by_email(self, email: str) -> UserDoc | None:
        return self._users.get(email.lower())

    async def create(
        self,
        *,
        email: str,
        display_name: str,
        password_hash: str,
        role: Literal["admin", "user"] = "user",
    ) -> UserDoc:
        doc: UserDoc = {
            "id": "u-" + uuid.uuid4().hex[:12],
            "email": email.lower(),
            "displayName": display_name,
            "passwordHash": password_hash,
            "role": role,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._users[email.lower()] = doc
        return doc

    async def count(self) -> int:
        return len(self._users)


class MongoUserStore:
    """Production Motor-backed store."""

    def __init__(self, mongo_url: str, db_name: str = "labmate") -> None:
        import motor.motor_asyncio  # imported lazily so tests never need Motor

        client = motor.motor_asyncio.AsyncIOMotorClient(mongo_url)
        self._col = client[db_name]["users"]

    async def find_by_email(self, email: str) -> UserDoc | None:
        doc = await self._col.find_one({"email": email.lower()}, {"_id": 0})
        return doc  # type: ignore[return-value]

    async def create(
        self,
        *,
        email: str,
        display_name: str,
        password_hash: str,
        role: Literal["admin", "user"] = "user",
    ) -> UserDoc:
        doc: UserDoc = {
            "id": "u-" + uuid.uuid4().hex[:12],
            "email": email.lower(),
            "displayName": display_name,
            "passwordHash": password_hash,
            "role": role,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        await self._col.insert_one({**doc})
        return doc

    async def count(self) -> int:
        return await self._col.count_documents({})


class SqliteUserStore:
    """File-backed store, durable across restarts. stdlib sqlite3 run in a thread
    so the async UserStore contract holds without blocking the event loop."""

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        parent = os.path.dirname(db_path) or "."
        os.makedirs(parent, mode=0o700, exist_ok=True)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                "id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, "
                "display_name TEXT, password_hash TEXT NOT NULL, "
                "role TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_doc(row: sqlite3.Row) -> UserDoc:
        return {
            "id": row["id"],
            "email": row["email"],
            "displayName": row["display_name"],
            "passwordHash": row["password_hash"],
            "role": row["role"],
            "createdAt": row["created_at"],
        }

    def _find_by_email(self, email: str) -> UserDoc | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, email, display_name, password_hash, role, created_at "
                "FROM users WHERE email = ?",
                (email,),
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_doc(row) if row is not None else None

    async def find_by_email(self, email: str) -> UserDoc | None:
        return await asyncio.to_thread(self._find_by_email, email.lower())

    def _insert(self, doc: UserDoc) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO users "
                "(id, email, display_name, password_hash, role, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    doc["id"],
                    doc["email"],
                    doc["displayName"],
                    doc["passwordHash"],
                    doc["role"],
                    doc["createdAt"],
                ),
            )
            conn.commit()
        finally:
            conn.close()

    async def create(
        self,
        *,
        email: str,
        display_name: str,
        password_hash: str,
        role: Literal["admin", "user"] = "user",
    ) -> UserDoc:
        doc: UserDoc = {
            "id": "u-" + uuid.uuid4().hex[:12],
            "email": email.lower(),
            "displayName": display_name,
            "passwordHash": password_hash,
            "role": role,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        await asyncio.to_thread(self._insert, doc)
        return doc

    def _count(self) -> int:
        conn = self._connect()
        try:
            return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        finally:
            conn.close()

    async def count(self) -> int:
        return await asyncio.to_thread(self._count)
