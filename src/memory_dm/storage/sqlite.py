"""A small SQLite store for sessions and turns."""

import sqlite3
from collections import OrderedDict
from importlib.resources import files
from pathlib import Path

from memory_dm.models import Session, Turn, date_bounds
from memory_dm.storage.experience_schema import install_experience_schema
from memory_dm.storage.indexing import index_text
from memory_dm.storage.profile_schema import install_profile_schema


class ConcurrentUpdateError(ValueError):
    pass


class SQLiteStore:
    def __init__(self, db_path: str | Path):
        # Connection-scoped, bounded by DenseIndex; never shared across databases or threads.
        self.dense_index_cache: OrderedDict = OrderedDict()
        self.derived_index_cache: OrderedDict = OrderedDict()
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(db_path), timeout=5.0)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.create_function("memory_terms", 1, index_text, deterministic=True)
        self._connection.create_function(
            "memory_date_start",
            1,
            lambda value: date_bounds(value)[0].isoformat() if value else None,
            deterministic=True,
        )
        self._connection.create_function(
            "memory_date_end",
            1,
            lambda value: date_bounds(value)[1].isoformat() if value else None,
            deterministic=True,
        )
        try:
            self.initialize()
        except Exception:
            self.close()
            raise

    def initialize(self) -> None:
        has_fts = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'atomic_memories_fts'"
        ).fetchone()
        has_raw_fts = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'conversation_turns_fts'"
        ).fetchone()
        schema = files("memory_dm.storage").joinpath("schema.sql").read_text(encoding="utf-8")
        self._connection.executescript(schema)
        install_experience_schema(self._connection)
        install_profile_schema(self._connection)
        if not has_fts:
            with self._connection:
                self._connection.execute(
                    """INSERT INTO atomic_memories_fts(rowid, memory_id, content)
                       SELECT rowid, memory_id, memory_terms(content) FROM atomic_memories"""
                )
        if not has_raw_fts:
            with self._connection:
                self._connection.execute(
                    """INSERT INTO conversation_turns_fts(rowid, turn_id, content)
                       SELECT rowid, turn_id, memory_terms(content) FROM conversation_turns"""
                )

    @property
    def connection(self) -> sqlite3.Connection:
        return self._connection

    def create_session(self, user_id: str) -> Session:
        session = Session(user_id=user_id)
        with self._connection:
            self._connection.execute(
                "INSERT INTO sessions (session_id, user_id, created_at) VALUES (?, ?, ?)",
                (session.session_id, session.user_id, session.created_at.isoformat()),
            )
        return session

    def get_session(self, session_id: str, user_id: str) -> Session | None:
        row = self._connection.execute(
            "SELECT * FROM sessions WHERE session_id = ? AND user_id = ?",
            (session_id, user_id),
        ).fetchone()
        return Session.model_validate(dict(row)) if row else None

    def list_sessions(self, user_id: str) -> list[Session]:
        rows = self._connection.execute(
            "SELECT * FROM sessions WHERE user_id = ? ORDER BY rowid", (user_id,)
        ).fetchall()
        return [Session.model_validate(dict(row)) for row in rows]

    def add_turn(
        self,
        turn: Turn,
        user_id: str,
        *,
        expected_history: list[Turn] | None = None,
        expected_memory_revision: int | None = None,
        expected_data_revision: tuple[int, int] | None = None,
    ) -> None:
        self._require_session(turn.session_id, user_id)
        with self._connection:
            if (
                expected_history is not None
                or expected_memory_revision is not None
                or expected_data_revision is not None
            ):
                self._connection.execute("BEGIN IMMEDIATE")
            if expected_history is not None:
                if self.get_turns(turn.session_id, user_id) != expected_history:
                    raise ConcurrentUpdateError(
                        "Conversation changed during the model reply; retry"
                    )
            if expected_memory_revision is not None:
                if self.memory_revision(user_id) != expected_memory_revision:
                    raise ConcurrentUpdateError("Memories changed during the model reply; retry")
            if (
                expected_data_revision is not None
                and self.data_revision(user_id) != expected_data_revision
            ):
                raise ConcurrentUpdateError(
                    "Memory or raw evidence changed during the model reply; retry"
                )
            self._connection.execute(
                """INSERT INTO conversation_turns
                   (turn_id, session_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)""",
                (
                    turn.turn_id,
                    turn.session_id,
                    turn.role,
                    turn.content,
                    turn.timestamp.isoformat(),
                ),
            )

    def get_turns(self, session_id: str, user_id: str) -> list[Turn]:
        self._require_session(session_id, user_id)
        rows = self._connection.execute(
            "SELECT * FROM conversation_turns WHERE session_id = ? ORDER BY rowid",
            (session_id,),
        ).fetchall()
        return [Turn.model_validate(dict(row)) for row in rows]

    def get_turn(self, turn_id: str, user_id: str) -> Turn | None:
        row = self._connection.execute(
            """SELECT t.* FROM conversation_turns AS t
               JOIN sessions AS s ON s.session_id = t.session_id
               WHERE t.turn_id = ? AND s.user_id = ?""",
            (turn_id, user_id),
        ).fetchone()
        return Turn.model_validate(dict(row)) if row else None

    def get_turns_by_ids(self, turn_ids: list[str], user_id: str) -> dict[str, Turn]:
        turns = {}
        for start in range(0, len(turn_ids), 400):
            chunk = turn_ids[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = self._connection.execute(
                f"""SELECT t.* FROM conversation_turns AS t
                    JOIN sessions AS s ON s.session_id = t.session_id
                    WHERE s.user_id = ? AND t.turn_id IN ({placeholders})""",
                [user_id, *chunk],
            ).fetchall()
            turns.update({row["turn_id"]: Turn.model_validate(dict(row)) for row in rows})
        return turns

    def memory_revision(self, user_id: str) -> int:
        row = self._connection.execute(
            "SELECT revision FROM retrieval_revisions WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row[0] if row else 0

    def invalidate_dense_cache(self, user_id: str) -> None:
        """Discard this user's in-process indexes after committing canonical changes."""
        for cache in (self.dense_index_cache, self.derived_index_cache):
            for key in list(cache):
                if key[0] == user_id:
                    del cache[key]

    def data_revision(self, user_id: str) -> tuple[int, int]:
        row = self._connection.execute(
            "SELECT revision FROM conversation_revisions WHERE user_id = ?", (user_id,)
        ).fetchone()
        return self.memory_revision(user_id), row[0] if row else 0

    def _require_session(self, session_id: str, user_id: str) -> None:
        if self.get_session(session_id, user_id) is None:
            raise ValueError("Session not found for this user")

    def close(self) -> None:
        self.dense_index_cache.clear()
        self.derived_index_cache.clear()
        self._connection.close()

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
