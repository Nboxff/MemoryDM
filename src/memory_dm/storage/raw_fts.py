"""Rebuildable FTS for raw turns; durable queues also track ordinary SQLite writers."""

from memory_dm.storage.indexing import index_text
from memory_dm.storage.sqlite import SQLiteStore


class RawFTSStore:
    def __init__(self, store: SQLiteStore):
        self.connection = store.connection

    def synchronize(self) -> None:
        if self.connection.execute("SELECT 1 FROM raw_fts_pending LIMIT 1").fetchone() is None:
            return
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.apply_pending()

    def apply_pending(self) -> None:
        if not self.connection.in_transaction:
            raise ValueError("Raw FTS synchronization requires an open write transaction")
        self.connection.execute(
            "DELETE FROM conversation_turns_fts "
            "WHERE turn_id IN (SELECT turn_id FROM raw_fts_pending)"
        )
        self.connection.execute(
            """INSERT INTO conversation_turns_fts(rowid, turn_id, content)
               SELECT t.rowid, t.turn_id, memory_terms(t.content) FROM conversation_turns AS t
               JOIN raw_fts_pending AS p ON p.turn_id = t.turn_id"""
        )
        self.connection.execute("DELETE FROM raw_fts_pending")

    def verify(self, user_id: str, original_turn_ids: list[str]) -> bool:
        rows = self.connection.execute(
            """SELECT t.turn_id, t.content, f.content AS indexed_content
               FROM conversation_turns AS t JOIN sessions AS s ON s.session_id = t.session_id
               LEFT JOIN conversation_turns_fts AS f ON f.turn_id = t.turn_id
               WHERE s.user_id = ?""",
            (user_id,),
        ).fetchall()
        if any(row["indexed_content"] != index_text(row["content"]) for row in rows):
            return False
        for start in range(0, len(original_turn_ids), 400):
            chunk = original_turn_ids[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            if self.connection.execute(
                f"""SELECT 1 FROM conversation_turns_fts AS f WHERE f.turn_id IN ({placeholders})
                    AND NOT EXISTS (SELECT 1 FROM conversation_turns AS t
                                    WHERE t.turn_id = f.turn_id)""",
                chunk,
            ).fetchone():
                return False
        return True

    def rebuild(self, user_id: str) -> int:
        self.synchronize()
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.rebuild_in_transaction(user_id)
        return self.connection.execute(
            """SELECT count(*) FROM conversation_turns AS t JOIN sessions AS s
               ON s.session_id = t.session_id WHERE s.user_id = ?""",
            (user_id,),
        ).fetchone()[0]

    def rebuild_in_transaction(self, user_id: str) -> None:
        if not self.connection.in_transaction:
            raise ValueError("Raw FTS rebuild requires an open write transaction")
        self.connection.execute(
            """DELETE FROM conversation_turns_fts WHERE turn_id IN (
                       SELECT t.turn_id FROM conversation_turns AS t JOIN sessions AS s
                       ON s.session_id = t.session_id WHERE s.user_id = ?)
                   OR NOT EXISTS (SELECT 1 FROM conversation_turns AS t
                                  WHERE t.turn_id = conversation_turns_fts.turn_id)""",
            (user_id,),
        )
        self.connection.execute(
            """INSERT INTO conversation_turns_fts(rowid, turn_id, content)
                   SELECT t.rowid, t.turn_id, memory_terms(t.content) FROM conversation_turns AS t
                   JOIN sessions AS s ON s.session_id = t.session_id WHERE s.user_id = ?""",
            (user_id,),
        )
