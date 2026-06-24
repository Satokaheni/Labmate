"""Reads codegraph SQLite, embeds symbols, upserts to Chroma code_symbols."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import aiosqlite

from services.memory.embedder import embed

log = logging.getLogger("codegraph_embedder")

POLL_SECS  = 5
BATCH_SIZE = 64
COLLECTION = "code_symbols"


def _db_path() -> Path:
    """Resolve DB_PATH lazily so WORKSPACE_PATH is read at call time, not import."""
    return Path(os.getenv("WORKSPACE_PATH", ".")) / ".codegraph" / "codegraph.db"


def node_to_text(row: dict) -> str:
    parts = [f"{row['kind']} {row['qualified_name'] or row['name']}"]
    if row["signature"]:
        parts.append(row["signature"])
    if row["docstring"]:
        parts.append(row["docstring"])
    parts.append(f"in {row['file_path']}")
    return "\n".join(parts)


class CodeGraphIndexer:
    def __init__(self, chroma_col) -> None:
        self._col = chroma_col
        self._seen_files: dict[str, object] = {}

    async def full_index(self) -> int:
        db_path = _db_path()
        if not db_path.exists():
            log.warning("codegraph DB not found at %s — skipping index", db_path)
            return 0

        # Skip if collection already has documents (avoids re-indexing on every restart)
        try:
            existing_count = await self._col.count()
            if existing_count > 0:
                log.info("full_index: collection has %d docs — skipping (use incremental)", existing_count)
                return existing_count
        except Exception:
            pass

        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, kind, name, qualified_name, file_path, language, "
                "start_line, end_line, signature, docstring FROM nodes"
            )
            rows = [dict(r) for r in await cursor.fetchall()]

        count = 0
        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            texts = [node_to_text(r) for r in batch]
            vecs  = await embed(texts)
            await self._col.upsert(
                ids        = [str(r["id"]) for r in batch],
                embeddings = vecs,
                documents  = texts,
                metadatas  = [{
                    "node_id":        str(r["id"]),
                    "file_path":      r["file_path"] or "",
                    "kind":           r["kind"] or "",
                    "name":           r["name"] or "",
                    "qualified_name": r["qualified_name"] or "",
                    "language":       r["language"] or "",
                    "start_line":     r["start_line"] or 0,
                    "end_line":       r["end_line"] or 0,
                } for r in batch],
            )
            count += len(batch)

        log.info("full_index: %d nodes embedded", count)
        return count

    async def _changed_files(self) -> list[str]:
        db_path = _db_path()
        if not db_path.exists():
            return []
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT path, indexed_at FROM files")
            rows = [dict(r) for r in await cur.fetchall()]

        changed = []
        for r in rows:
            prev = self._seen_files.get(r["path"])
            if prev is None or r["indexed_at"] != prev:
                self._seen_files[r["path"]] = r["indexed_at"]
                changed.append(r["path"])
        return changed

    async def incremental_update(self, changed_paths: list[str]) -> None:
        async with aiosqlite.connect(_db_path()) as db:
            db.row_factory = aiosqlite.Row
            placeholders = ",".join("?" * len(changed_paths))
            cur = await db.execute(
                f"SELECT id, kind, name, qualified_name, file_path, language, "
                f"start_line, end_line, signature, docstring FROM nodes "
                f"WHERE file_path IN ({placeholders})",
                changed_paths,
            )
            rows = [dict(r) for r in await cur.fetchall()]

        new_ids = {str(r["id"]) for r in rows}

        if rows:
            texts = [node_to_text(r) for r in rows]
            vecs  = await embed(texts)
            # Upsert new vectors FIRST — if this fails, old vectors remain (no data loss)
            await self._col.upsert(
                ids        = [str(r["id"]) for r in rows],
                embeddings = vecs,
                documents  = texts,
                metadatas  = [{
                    "node_id":        str(r["id"]),
                    "file_path":      r["file_path"] or "",
                    "kind":           r["kind"] or "",
                    "name":           r["name"] or "",
                    "qualified_name": r["qualified_name"] or "",
                    "language":       r["language"] or "",
                    "start_line":     r["start_line"] or 0,
                    "end_line":       r["end_line"] or 0,
                } for r in rows],
            )
            log.info("incremental_update: %d nodes re-embedded from %d files",
                     len(rows), len(changed_paths))

        # Delete orphaned vectors (nodes removed from these files) AFTER upsert succeeds
        for path in changed_paths:
            existing = await self._col.get(where={"file_path": path})
            orphan_ids = [eid for eid in existing["ids"] if eid not in new_ids]
            if orphan_ids:
                await self._col.delete(ids=orphan_ids)

    async def watch(self) -> None:
        while True:
            await asyncio.sleep(POLL_SECS)
            try:
                changed = await self._changed_files()
                if changed:
                    await self.incremental_update(changed)
            except Exception as exc:
                log.warning("poll error (non-fatal): %s", exc)
