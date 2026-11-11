"""SQLite snapshots and atomic commits for incremental memory maintenance.

No model calls or reconciliation choices belong here. The caller supplies a write plan;
this adapter verifies its source snapshot and commits facts, provenance, FTS and progress.
"""

import hashlib
import json

from memory_dm.errors import ReconciliationError
from memory_dm.memory.state import WritePlan, WriteSnapshot
from memory_dm.models import AtomicMemory, Session, Turn, ValidTime, utc_now
from memory_dm.storage.atomic import AtomicMemoryStore
from memory_dm.storage.fts import FTSIndexStore
from memory_dm.storage.sqlite import SQLiteStore


class MemoryWriteRepository:
    def __init__(self, store: SQLiteStore):
        self.store = store
        self.connection = store.connection
        self.atomic = AtomicMemoryStore(store)

    def _read(self, user_id: str) -> WriteSnapshot:
        memories = {memory.id: memory for memory in self.atomic.list(user_id)}
        rows = self.connection.execute(
            """SELECT t.* FROM conversation_turns AS t JOIN sessions AS s
               ON s.session_id = t.session_id WHERE s.user_id = ? ORDER BY t.rowid""",
            (user_id,),
        ).fetchall()
        processed = self.connection.execute(
            """SELECT p.turn_id FROM memory_processed_turns AS p JOIN conversation_turns AS t
               ON t.turn_id = p.turn_id JOIN sessions AS s ON s.session_id = t.session_id
               WHERE s.user_id = ?""",
            (user_id,),
        ).fetchall()
        return WriteSnapshot(
            memories,
            {r["turn_id"]: Turn.model_validate(dict(r)) for r in rows},
            {r[0] for r in processed},
            self.store.memory_revision(user_id),
        )

    def snapshot(self, user_id: str) -> WriteSnapshot:
        if not user_id.strip() or self.connection.in_transaction:
            raise ReconciliationError("Writing requires a user and no open transaction")
        with self.connection:
            self.connection.execute("BEGIN")
            return self._read(user_id)

    def read_in_transaction(self, user_id: str) -> WriteSnapshot:
        """Share a canonical source snapshot with dependent-layer transactions."""
        if not self.connection.in_transaction:
            raise ReconciliationError("Reading a shared source snapshot requires a transaction")
        return self._read(user_id)

    def pending(self, session: Session) -> list[Turn]:
        turns = self.store.get_turns(session.session_id, session.user_id)
        processed = {
            row[0]
            for row in self.connection.execute(
                """SELECT p.turn_id FROM memory_processed_turns AS p
                   JOIN conversation_turns AS t ON t.turn_id = p.turn_id
                   WHERE t.session_id = ?""",
                (session.session_id,),
            )
        }
        return [turn for turn in turns if turn.turn_id not in processed]

    @staticmethod
    def maintenance_key(before: WriteSnapshot, session: Session) -> str:
        """Invalidate retry counts after a source edit, deletion or processing progress."""
        rows = [
            (turn.model_dump(mode="json"), turn.turn_id in before.processed)
            for turn in before.turns.values()
            if turn.session_id == session.session_id
        ]
        return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()

    def record_failure(self, session: Session, input_hash: str, error: Exception) -> None:
        # An in-flight failure must not consume retries for subsequently changed input.
        current = self.snapshot(session.user_id)
        current_key = self.maintenance_key(current, session)
        consumed_attempt = int(current_key == input_hash)
        diagnostics = getattr(error, "diagnostics", None) or {
            "operation": "memory_maintenance",
            "issues": [{"code": type(error).__name__}],
        }
        with self.connection:
            self.connection.execute(
                """INSERT INTO memory_maintenance_failures VALUES (?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     attempts = CASE WHEN input_hash = excluded.input_hash
                                     THEN attempts + excluded.attempts ELSE excluded.attempts END,
                     input_hash = excluded.input_hash, diagnostics = excluded.diagnostics""",
                (session.session_id, current_key, consumed_attempt, json.dumps(diagnostics)),
            )

    def complete_maintenance(self, session: Session) -> None:
        row = self.connection.execute(
            "SELECT 1 FROM memory_maintenance_failures WHERE session_id = ?", (session.session_id,)
        ).fetchone()
        if row is None:
            return
        pending = self.pending(session)
        with self.connection:
            if not pending:
                self.connection.execute(
                    "DELETE FROM memory_maintenance_failures WHERE session_id = ?",
                    (session.session_id,),
                )
            else:
                # A successful recovery chunk leaves later chunks scheduled, not lost.
                key = self.maintenance_key(self._read(session.user_id), session)
                self.connection.execute(
                    "UPDATE memory_maintenance_failures SET input_hash = ?, attempts = 0, "
                    "diagnostics = '{}' WHERE session_id = ?",
                    (key, session.session_id),
                )

    def recovery_sessions(
        self, user_id: str, *, exclude_session_id: str, max_attempts: int = 3
    ) -> list[Session]:
        rows = self.connection.execute(
            """SELECT s.*, f.input_hash, f.attempts FROM memory_maintenance_failures f
               JOIN sessions s ON s.session_id = f.session_id
               WHERE s.user_id = ? AND s.session_id != ?
               ORDER BY f.attempts, f.rowid LIMIT 50""",
            (user_id, exclude_session_id),
        ).fetchall()
        before = self.snapshot(user_id)
        result = []
        for row in rows:
            session = Session.model_validate(
                {key: row[key] for key in ("session_id", "user_id", "created_at")}
            )
            if not any(
                t.session_id == session.session_id and key not in before.processed
                for key, t in before.turns.items()
            ):
                self.complete_maintenance(session)
                continue
            if row["attempts"] < max_attempts or row["input_hash"] != self.maintenance_key(
                before, session
            ):
                result.append(session)
        return result

    def pending_recovery_sessions(self, user_id: str) -> list[str]:
        # Keep exhausted work visible even when it no longer consumes automatic retries.
        return [
            row[0]
            for row in self.connection.execute(
                """SELECT f.session_id FROM memory_maintenance_failures f
               JOIN sessions s ON s.session_id = f.session_id
               WHERE s.user_id = ? ORDER BY f.rowid LIMIT 50""",
                (user_id,),
            )
        ]

    def commit(self, user_id: str, before: WriteSnapshot, plan: WritePlan) -> None:
        # API work has finished. Recheck under the write lock, then commit all dependent
        # records together. Calling public CRUD methods here would commit too early.
        if self.connection.in_transaction:
            raise ReconciliationError(
                "Memory commit requires a connection without an open transaction"
            )
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if self._read(user_id) != before:
                raise ReconciliationError("User data changed during reconciliation; retry")
            self._validate_plan(user_id, before, plan)
            for memory in plan.upserts:
                self._upsert(memory, before.memories.get(memory.id))
            # Attribute corrections to the session doing the write even when their
            # surviving evidence still points to an earlier conversation.
            sessions = {before.turns[key].session_id for key in plan.processed_turn_ids}
            self.connection.executemany(
                "INSERT OR IGNORE INTO experience_session_changes VALUES (?, ?)",
                [
                    (session_id, memory.id)
                    for session_id in sorted(sessions)
                    for memory in plan.upserts
                ],
            )
            self.connection.executemany(
                "INSERT OR IGNORE INTO memory_links(source_id, target_id, operation) "
                "VALUES (?, ?, ?)",
                [(link.source_id, link.target_id, link.operation) for link in plan.links],
            )
            self.connection.executemany(
                "INSERT INTO memory_processed_turns(turn_id) VALUES (?)",
                [(key,) for key in plan.processed_turn_ids],
            )
            FTSIndexStore(self.store).apply_pending()
        self.store.invalidate_dense_cache(user_id)

    @staticmethod
    def _validate_plan(user_id: str, before: WriteSnapshot, plan: WritePlan) -> None:
        if any(memory.user_id != user_id for memory in plan.upserts):
            raise ReconciliationError("Write plan belongs to another user")
        if any(
            key not in before.turns or key in before.processed for key in plan.processed_turn_ids
        ):
            raise ReconciliationError(
                "Processing checkpoint refers to unavailable or processed turns"
            )
        owned = set(before.memories) | {memory.id for memory in plan.upserts}
        if any(link.source_id not in owned or link.target_id not in owned for link in plan.links):
            raise ReconciliationError("Write links must belong to the current user")

    def _upsert(self, memory: AtomicMemory, original: AtomicMemory | None) -> None:
        memory = self.atomic._validate(memory)
        if original is None:
            self.atomic._insert(memory)
            return
        time = memory.valid_time or ValidTime()
        self.connection.execute(
            """UPDATE atomic_memories SET content = ?, memory_type = ?, status = ?,
               valid_start = ?, valid_end = ?, valid_point = ?, updated_at = ?,
               version = version + 1 WHERE memory_id = ? AND user_id = ?""",
            (
                memory.content,
                memory.type,
                memory.status,
                time.start,
                time.end,
                time.point,
                utc_now().isoformat(),
                memory.id,
                memory.user_id,
            ),
        )
        if memory.evidence != original.evidence:
            self.connection.execute("DELETE FROM memory_evidence WHERE memory_id = ?", (memory.id,))
            self.atomic._insert_evidence(memory.id, memory.evidence)
        if memory.content != original.content:
            self.connection.execute(
                """DELETE FROM memory_embeddings WHERE vector_id IN
                   (SELECT vector_id FROM memory_vector_ids WHERE memory_id = ?)""",
                (memory.id,),
            )
