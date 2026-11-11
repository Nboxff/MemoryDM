"""Atomic memory CRUD and transactional extraction writes."""

from __future__ import annotations

import sqlite3

from pydantic import TypeAdapter

from memory_dm.memory.validator import MemoryValidator
from memory_dm.models import AtomicMemory, Evidence, MemoryStatus, Turn, ValidTime, utc_now
from memory_dm.storage.sqlite import SQLiteStore

_STATUS_ADAPTER = TypeAdapter(MemoryStatus)


class AtomicMemoryStore:
    def __init__(self, store: SQLiteStore):
        self.store = store
        self.connection = store.connection
        self.validator = MemoryValidator()

    def add(self, memory: AtomicMemory) -> str:
        with self.connection:
            memory = self._validate(memory)
            self._insert(memory)
        return memory.id

    def get(self, memory_id: str, user_id: str) -> AtomicMemory | None:
        row = self.connection.execute(
            "SELECT * FROM atomic_memories WHERE memory_id = ? AND user_id = ?",
            (memory_id, user_id),
        ).fetchone()
        return self._from_row(row) if row else None

    def list(self, user_id: str, session_id: str | None = None) -> list[AtomicMemory]:
        parameters = [user_id]
        query = "SELECT m.* FROM atomic_memories AS m WHERE m.user_id = ?"
        if session_id is not None:
            if self.store.get_session(session_id, user_id) is None:
                raise ValueError("Session not found for this user")
            query += """ AND EXISTS (SELECT 1 FROM memory_evidence AS e
                        WHERE e.memory_id = m.memory_id AND e.session_id = ?)"""
            parameters.append(session_id)
        rows = self.connection.execute(query + " ORDER BY m.rowid", parameters).fetchall()
        return [self._from_row(row) for row in rows]

    def update(self, memory: AtomicMemory, user_id: str) -> None:
        with self.connection:
            if memory.user_id != user_id or self.get(memory.id, user_id) is None:
                raise ValueError("Memory not found for this user")
            memory = self._validate(memory)
            time = memory.valid_time or ValidTime()
            self.connection.execute(
                """UPDATE atomic_memories SET content = ?, memory_type = ?,
                   valid_start = ?, valid_end = ?, valid_point = ?, status = ?, updated_at = ?,
                   version = version + 1 WHERE memory_id = ? AND user_id = ?""",
                (
                    memory.content,
                    memory.type,
                    time.start,
                    time.end,
                    time.point,
                    memory.status,
                    utc_now().isoformat(),
                    memory.id,
                    user_id,
                ),
            )
            self.connection.execute("DELETE FROM memory_evidence WHERE memory_id = ?", (memory.id,))
            self._insert_evidence(memory.id, memory.evidence)

    def active_conflicts(self, memory_id: str, user_id: str) -> list[AtomicMemory]:
        """Read active KEEP_BOTH counterparts, scoping both sides to the same user."""
        rows = self.connection.execute(
            """SELECT DISTINCT m.* FROM memory_links AS l
               JOIN atomic_memories AS owner ON owner.memory_id = ?
               JOIN atomic_memories AS m ON m.memory_id = CASE
                   WHEN l.source_id = owner.memory_id THEN l.target_id ELSE l.source_id END
               WHERE l.operation = 'KEEP_BOTH'
               AND (l.source_id = owner.memory_id OR l.target_id = owner.memory_id)
               AND owner.user_id = ? AND owner.status = 'ACTIVE'
               AND m.user_id = ? AND m.status = 'ACTIVE'""",
            (memory_id, user_id, user_id),
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def delete(self, memory_id: str, user_id: str) -> bool:
        with self.connection:
            if self.get(memory_id, user_id) is None:
                return False
            self.connection.execute("DELETE FROM memory_evidence WHERE memory_id = ?", (memory_id,))
            cursor = self.connection.execute(
                "DELETE FROM atomic_memories WHERE memory_id = ? AND user_id = ?",
                (memory_id, user_id),
            )
        return cursor.rowcount == 1

    def set_status(self, memory_id: str, user_id: str, status: MemoryStatus) -> None:
        status = _STATUS_ADAPTER.validate_python(status)
        # A single scoped UPDATE preserves concurrent edits to content, dates and evidence.
        with self.connection:
            cursor = self.connection.execute(
                """UPDATE atomic_memories SET status = ?, updated_at = ?, version = version + 1
                   WHERE memory_id = ? AND user_id = ?""",
                (status, utc_now().isoformat(), memory_id, user_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Memory not found for this user")

    def save_batch(
        self, memories: list[AtomicMemory], user_id: str, *, source_turns: list[Turn] | None = None
    ) -> list[AtomicMemory]:
        saved_ids = []
        with self.connection:
            if source_turns is not None:
                self.connection.execute("BEGIN IMMEDIATE")
                for turn in source_turns:
                    if self.store.get_turn(turn.turn_id, user_id) != turn:
                        raise ValueError("Source conversation changed during extraction; retry")
            for memory in memories:
                if memory.user_id != user_id:
                    raise ValueError("Extracted memory belongs to another user")
                memory = self._validate(memory)
                time = memory.valid_time or ValidTime()
                row = self.connection.execute(
                    """SELECT * FROM atomic_memories WHERE user_id = ? AND content = ?
                       AND memory_type = ? AND valid_start IS ? AND valid_end IS ?
                       AND valid_point IS ? AND status = ? ORDER BY rowid LIMIT 1""",
                    (
                        user_id,
                        memory.content,
                        memory.type,
                        time.start,
                        time.end,
                        time.point,
                        memory.status,
                    ),
                ).fetchone()
                if row is None:
                    self._insert(memory)
                    memory_id = memory.id
                else:
                    existing = self._from_row(row)
                    additions = [item for item in memory.evidence if item not in existing.evidence]
                    if additions:
                        self._insert_evidence(existing.id, additions)
                        self.connection.execute(
                            """UPDATE atomic_memories SET updated_at = ?, version = version + 1
                               WHERE memory_id = ?""",
                            (utc_now().isoformat(), existing.id),
                        )
                    memory_id = existing.id
                if memory_id not in saved_ids:
                    saved_ids.append(memory_id)
        return [self.get(memory_id, user_id) for memory_id in saved_ids]

    def _validate(self, memory: AtomicMemory) -> AtomicMemory:
        turns = []
        for evidence in memory.evidence:
            turn = self.store.get_turn(evidence.turn_id, memory.user_id)
            if turn is None:
                raise ValueError("Evidence turn not found for this user")
            turns.append(turn)
        memory = self.validator.validate(memory, turns)
        memory.evidence = list(
            {
                (item.session_id, item.turn_id, item.span_start, item.span_end): item
                for item in memory.evidence
            }.values()
        )
        return memory

    def _insert(self, memory: AtomicMemory) -> None:
        time = memory.valid_time or ValidTime()
        self.connection.execute(
            """INSERT INTO atomic_memories
               (memory_id, user_id, content, memory_type, valid_start, valid_end,
                valid_point, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                memory.id,
                memory.user_id,
                memory.content,
                memory.type,
                time.start,
                time.end,
                time.point,
                memory.status,
                memory.created_at.isoformat(),
                memory.created_at.isoformat(),
            ),
        )
        self._insert_evidence(memory.id, memory.evidence)

    def _insert_evidence(self, memory_id: str, evidence: list[Evidence]) -> None:
        self.connection.executemany(
            """INSERT INTO memory_evidence
               (memory_id, session_id, turn_id, span_start, span_end) VALUES (?, ?, ?, ?, ?)""",
            [
                (memory_id, item.session_id, item.turn_id, item.span_start, item.span_end)
                for item in evidence
            ],
        )

    def _from_row(self, row: sqlite3.Row) -> AtomicMemory:
        sources = self.connection.execute(
            """SELECT session_id, turn_id, span_start, span_end FROM memory_evidence
               WHERE memory_id = ? ORDER BY evidence_id""",
            (row["memory_id"],),
        ).fetchall()
        time = ValidTime(start=row["valid_start"], end=row["valid_end"], point=row["valid_point"])
        return AtomicMemory(
            id=row["memory_id"],
            user_id=row["user_id"],
            content=row["content"],
            type=row["memory_type"],
            created_at=row["created_at"],
            status=row["status"],
            valid_time=time if any((time.start, time.end, time.point)) else None,
            evidence=[Evidence.model_validate(dict(source)) for source in sources],
        )
