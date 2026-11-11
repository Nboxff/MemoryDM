"""Literal keyword retrieval over SQLite FTS5 with BM25 ordering."""

from __future__ import annotations

from typing import TYPE_CHECKING

from memory_dm.storage.indexing import match_query
from memory_dm.storage.search import search_predicate
from memory_dm.storage.sqlite import SQLiteStore

if TYPE_CHECKING:
    from memory_dm.retrieval.filters import SearchFilters


class FTSIndexStore:
    def __init__(self, store: SQLiteStore):
        self.connection = store.connection

    def search(
        self, query: str, filters: SearchFilters, top_k: int, *, natural_language: bool = False
    ) -> list[tuple[str, float]]:
        self.synchronize()
        expression = match_query(query, natural_language=natural_language)
        if not expression:
            return []
        where, parameters = search_predicate(filters)
        rows = self.connection.execute(
            f"""SELECT m.memory_id, bm25(atomic_memories_fts) AS score
                FROM atomic_memories_fts JOIN atomic_memories AS m
                  ON m.memory_id = atomic_memories_fts.memory_id
                WHERE atomic_memories_fts MATCH ? AND {where}
                ORDER BY score, m.memory_id LIMIT ?""",
            [expression, *parameters, top_k],
        ).fetchall()
        return [(row["memory_id"], -row["score"]) for row in rows]

    def synchronize(self) -> None:
        # The durable queue also records writes from ordinary SQLite connections.
        if self.connection.execute("SELECT 1 FROM fts_pending LIMIT 1").fetchone() is None:
            return
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.apply_pending()

    def apply_pending(self) -> None:
        """Synchronize in the caller's existing write transaction without committing it."""
        if not self.connection.in_transaction:
            raise ValueError("FTS synchronization requires an open write transaction")
        self.connection.execute(
            """DELETE FROM atomic_memories_fts
               WHERE memory_id IN (SELECT memory_id FROM fts_pending)"""
        )
        self.connection.execute(
            """INSERT INTO atomic_memories_fts(rowid, memory_id, content)
               SELECT m.rowid, m.memory_id, memory_terms(m.content)
               FROM atomic_memories AS m JOIN fts_pending AS p ON p.memory_id = m.memory_id"""
        )
        self.connection.execute("DELETE FROM fts_pending")

    def rebuild(self, user_id: str) -> int:
        self.synchronize()
        with self.connection:
            self.connection.execute(
                """DELETE FROM atomic_memories_fts WHERE memory_id IN
                   (SELECT memory_id FROM atomic_memories WHERE user_id = ?)
                   OR NOT EXISTS (SELECT 1 FROM atomic_memories AS m
                                  WHERE m.memory_id = atomic_memories_fts.memory_id)""",
                (user_id,),
            )
            self.connection.execute(
                """INSERT INTO atomic_memories_fts(rowid, memory_id, content)
                   SELECT rowid, memory_id, memory_terms(content)
                   FROM atomic_memories WHERE user_id = ?""",
                (user_id,),
            )
        return self.connection.execute(
            "SELECT count(*) FROM atomic_memories WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
