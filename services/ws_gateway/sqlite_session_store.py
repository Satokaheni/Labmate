from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_KV_NS = "gw"


class SqliteSessionStore:
    """LocalStore(SQLite)-backed session store. chat_turns is the single turn
    store (frontend history + orchestrator continuity read the same rows).
    Session metadata (title/mode/debug) lives in session_kv under one JSON blob.
    """

    def __init__(self, store, *, local_user: str = "u-local") -> None:
        self._store = store
        self._user = local_user

    async def _meta(self, sid: str) -> dict:
        raw = await self._store.session_kv_get(_KV_NS, sid)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {}

    async def _set_meta(self, sid: str, meta: dict) -> None:
        await self._store.session_kv_set(_KV_NS, sid, json.dumps(meta))

    async def _session_dict(self, sid: str, row: dict, meta: dict) -> dict:
        turns = await self._store.turns_with_payload(sid)
        return {
            "id": sid,
            "title": meta.get("title", ""),
            "mode": meta.get("mode", "chat"),
            "turnCount": len(turns),
            "contextTokens": meta.get("contextTokens", 0),
            "createdAt": row.get("created_at") or meta.get("createdAt", ""),
            "updatedAt": meta.get("updatedAt") or row.get("created_at", ""),
        }

    async def create(
        self, *, title: str, mode: str, session_id: str | None = None, updated_at: str | None = None
    ) -> dict:
        sid = session_id or "s-" + uuid.uuid4().hex[:12]
        now = updated_at or _now_iso()
        await self._store.record_session(
            sid, user_id=self._user, task_preview=title, created_at=now
        )
        await self._set_meta(
            sid,
            {
                "title": title,
                "mode": mode,
                "debug": False,
                "contextTokens": 0,
                "createdAt": now,
                "updatedAt": now,
            },
        )
        return {
            "id": sid,
            "title": title,
            "mode": mode,
            "turnCount": 0,
            "contextTokens": 0,
            "createdAt": now,
            "updatedAt": now,
        }

    async def list(self) -> list[dict]:
        rows = await self._store.list_sessions(self._user, limit=200)
        out = [
            await self._session_dict(r["session_id"], r, await self._meta(r["session_id"]))
            for r in rows
        ]
        return sorted(out, key=lambda s: s["updatedAt"], reverse=True)

    async def get(self, sid: str) -> dict | None:
        rows = await self._store.list_sessions(self._user, limit=1000)
        row = next((r for r in rows if r["session_id"] == sid), None)
        if row is None:
            return None
        return await self._session_dict(sid, row, await self._meta(sid))

    async def rename(self, sid: str, title: str) -> dict | None:
        meta = await self._meta(sid)
        if not meta:
            return None
        meta["title"] = title
        meta["updatedAt"] = _now_iso()
        await self._set_meta(sid, meta)
        return await self.get(sid)

    async def delete(self, sid: str) -> bool:
        existing = await self.get(sid)
        if existing is None:
            return False
        await self._store.delete_session(sid)
        return True

    async def turns(self, sid: str) -> list[dict]:
        out = []
        for r in await self._store.turns_with_payload(sid):
            if r["payload"] is not None:
                turn = dict(r["payload"])
                turn["seq"] = r["seq"]
            else:
                turn = {
                    "id": f"{sid}-{r['seq']}",
                    "sessionId": sid,
                    "role": r["role"],
                    "text": r["text"],
                    "createdAt": r["created_at"],
                    "status": "complete",
                    "seq": r["seq"],
                }
            out.append(turn)
        return out

    async def add_turn(self, sid: str, turn: dict) -> None:
        await self._store.append_turn_payload(
            sid,
            role=turn.get("role", ""),
            text=turn.get("text", ""),
            payload=turn,
        )
        meta = await self._meta(sid)
        if meta:
            meta["updatedAt"] = _now_iso()
            await self._set_meta(sid, meta)

    async def set_debug(self, sid: str, enabled: bool) -> None:
        meta = await self._meta(sid)
        meta["debug"] = enabled
        await self._set_meta(sid, meta)

    async def get_debug(self, sid: str) -> bool:
        return bool((await self._meta(sid)).get("debug", False))
