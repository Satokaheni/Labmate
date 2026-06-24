from __future__ import annotations

import time
import uuid
from typing import Literal, Optional, Protocol, TypedDict


class UserDoc(TypedDict):
    id: str
    email: str
    displayName: str
    passwordHash: str
    role: Literal["admin", "user"]
    createdAt: str


class UserStore(Protocol):
    async def find_by_email(self, email: str) -> Optional[UserDoc]: ...
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

    async def find_by_email(self, email: str) -> Optional[UserDoc]:
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

    async def find_by_email(self, email: str) -> Optional[UserDoc]:
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
