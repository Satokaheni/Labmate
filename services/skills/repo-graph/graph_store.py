import json
import logging
import os
import sqlite3
import sys

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("repo-graph")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS definitions (
    file   TEXT NOT NULL,
    line   INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    PRIMARY KEY (file, symbol, line)
);
CREATE TABLE IF NOT EXISTS edges (
    src_file   TEXT NOT NULL,
    src_line   INTEGER NOT NULL,
    src_symbol TEXT NOT NULL,
    dst_file   TEXT NOT NULL,
    dst_line   INTEGER NOT NULL,
    dst_symbol TEXT NOT NULL,
    kind       TEXT NOT NULL,
    PRIMARY KEY (src_file, src_line, dst_file, dst_line, dst_symbol, kind)
);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_symbol, dst_file);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_symbol, src_file);
CREATE INDEX IF NOT EXISTS idx_defs_symbol ON definitions(symbol);
"""


class GraphStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        log.info("GraphStore opened at %s", db_path)

    def close(self) -> None:
        self._conn.close()

    def upsert_definitions(self, defs: list[dict]) -> None:
        self._conn.execute("DELETE FROM definitions")
        self._conn.executemany(
            "INSERT OR REPLACE INTO definitions (file, line, symbol) VALUES (?, ?, ?)",
            [(d["file"], d["line"], d["symbol"]) for d in defs],
        )
        self._conn.commit()

    def upsert_edges(self, edges: list) -> None:
        self._conn.execute("DELETE FROM edges")
        self._conn.executemany(
            """INSERT OR REPLACE INTO edges
               (src_file, src_line, src_symbol, dst_file, dst_line, dst_symbol, kind)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [(e.src_file, e.src_line, e.src_symbol,
              e.dst_file, e.dst_line, e.dst_symbol, e.kind) for e in edges],
        )
        self._conn.commit()
        log.info("upserted %d edges", len(edges))

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        rows = self._conn.execute(
            """SELECT file, line, symbol FROM definitions
               WHERE symbol LIKE ?
               ORDER BY (symbol = ?) DESC, length(symbol) ASC
               LIMIT ?""",
            (f"%{query}%", query, top_k),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_references(self, file: str, symbol: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT src_file, src_line, src_symbol, kind
               FROM edges WHERE dst_symbol = ? AND dst_file = ?
               ORDER BY src_file, src_line""",
            (symbol, file),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_callers(self, file: str, symbol: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT src_file, src_line, src_symbol, kind
               FROM edges WHERE dst_symbol = ? AND dst_file = ? AND kind = 'call'
               ORDER BY src_file, src_line""",
            (symbol, file),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_callees(self, file: str, symbol: str) -> list[dict]:
        rows = self._conn.execute(
            """SELECT dst_file, dst_line, dst_symbol, kind
               FROM edges WHERE src_symbol = ? AND src_file = ? AND kind = 'call'
               ORDER BY dst_file, dst_line""",
            (symbol, file),
        ).fetchall()
        return [dict(r) for r in rows]
