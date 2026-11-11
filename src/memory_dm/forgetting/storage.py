"""Source snapshots and atomic source/experience repairs without forgotten-text audits."""

import sqlite3
from dataclasses import dataclass, field

from memory_dm.experience.models import ExperienceMemory
from memory_dm.forgetting.models import ForgettingError
from memory_dm.models import AtomicMemory, Evidence, Session, Turn, ValidTime, utc_now
from memory_dm.profile.models import UserProfile
from memory_dm.storage.atomic import AtomicMemoryStore
from memory_dm.storage.experience import ExperienceStore
from memory_dm.storage.indexing import index_text
from memory_dm.storage.maintenance import MemoryWriteRepository
from memory_dm.storage.profile import ProfileStore
from memory_dm.storage.raw_fts import RawFTSStore
from memory_dm.storage.sqlite import SQLiteStore


@dataclass
class ForgetSnapshot:
    memories: dict[str, AtomicMemory]
    turns: dict[str, Turn]
    signature: tuple
    experiences: dict[str, ExperienceMemory] = field(default_factory=dict)
    experience_cutoff: int = 0
    profiles: dict[str, UserProfile] = field(default_factory=dict)
    processed_turns: set[str] = field(default_factory=set)
    partial: bool = False


class ForgetRepository:
    def __init__(self, store: SQLiteStore):
        self.store = store
        self.connection = store.connection

    def read(self, user_id: str) -> ForgetSnapshot:
        rows = self.connection.execute(
            "SELECT * FROM atomic_memories WHERE user_id = ? ORDER BY memory_id", (user_id,)
        ).fetchall()
        raw = self.connection.execute(
            """SELECT t.* FROM conversation_turns AS t JOIN sessions AS s
               ON s.session_id = t.session_id WHERE s.user_id = ? ORDER BY t.rowid""",
            (user_id,),
        ).fetchall()
        evidence = self.connection.execute(
            """SELECT e.* FROM memory_evidence AS e JOIN atomic_memories AS m
               ON m.memory_id = e.memory_id WHERE m.user_id = ? ORDER BY e.evidence_id""",
            (user_id,),
        ).fetchall()
        turns = {row["turn_id"]: Turn.model_validate(dict(row)) for row in raw}
        sources = {}
        for row in evidence:
            if row["turn_id"] not in turns:
                raise ForgettingError(
                    "Memory evidence belongs to a missing or different user's turn"
                )
            item = Evidence(
                **{name: row[name] for name in ("session_id", "turn_id", "span_start", "span_end")}
            )
            sources.setdefault(row["memory_id"], []).append(item)
        memories = {}
        for row in rows:
            time = ValidTime(
                start=row["valid_start"], end=row["valid_end"], point=row["valid_point"]
            )
            memories[row["memory_id"]] = AtomicMemory(
                id=row["memory_id"],
                user_id=row["user_id"],
                content=row["content"],
                type=row["memory_type"],
                status=row["status"],
                created_at=row["created_at"],
                valid_time=time if any((time.start, time.end, time.point)) else None,
                evidence=sources.get(row["memory_id"], []),
            )
        experience_queries = (
            "SELECT * FROM experience_memories WHERE user_id = ? ORDER BY experience_id",
            "SELECT g.* FROM experience_groups g JOIN experience_memories e "
            "ON e.experience_id = g.experience_id WHERE e.user_id = ? ORDER BY g.group_id",
            "SELECT p.* FROM experience_sources p JOIN experience_groups g "
            "ON g.group_id = p.group_id "
            "JOIN experience_memories e ON e.experience_id = g.experience_id "
            "WHERE e.user_id = ? ORDER BY p.group_id, p.memory_id",
            "SELECT * FROM experience_pending WHERE user_id = ? ORDER BY memory_id",
            "SELECT * FROM experience_turn_epochs WHERE user_id = ? ORDER BY epoch",
            "SELECT * FROM experience_revocations WHERE user_id = ? ORDER BY revocation_id",
            "SELECT s.* FROM experience_revoked_sources s JOIN experience_revocations r "
            "ON r.revocation_id = s.revocation_id WHERE r.user_id = ? "
            "ORDER BY s.revocation_id, s.turn_id",
            "SELECT * FROM user_profiles WHERE user_id = ?",
        )
        extra = [
            self.connection.execute(query, (user_id,)).fetchall() for query in experience_queries
        ]
        processed = self.connection.execute(
            "SELECT p.turn_id FROM memory_processed_turns p "
            "JOIN conversation_turns t ON t.turn_id=p.turn_id "
            "JOIN sessions s ON s.session_id=t.session_id WHERE s.user_id=? ORDER BY p.turn_id",
            (user_id,),
        ).fetchall()
        signature = tuple(
            tuple(tuple(row) for row in group) for group in (rows, raw, evidence, *extra, processed)
        )
        cutoff = self.connection.execute(
            "SELECT COALESCE(MAX(epoch), 0) FROM experience_turn_epochs WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
        return ForgetSnapshot(
            memories,
            turns,
            signature,
            ExperienceStore(self.store).read(user_id),
            cutoff,
            ProfileStore(self.store).read(user_id),
            {row[0] for row in processed},
        )

    def snapshot(self, user_id: str) -> ForgetSnapshot:
        if self.connection.in_transaction:
            raise ForgettingError("Forgetting requires a connection without an open transaction")
        with self.connection:
            self.connection.execute("BEGIN")
            return self.read(user_id)

    def apply(
        self,
        user_id: str,
        before: ForgetSnapshot,
        memories: dict,
        turns: dict,
        *,
        new_session: Session | None = None,
        experiences: dict[str, ExperienceMemory] | None = None,
        profiles: dict[str, UserProfile] | None = None,
    ) -> bool:
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.read(user_id).signature != before.signature:
                raise ForgettingError("User data changed during forgetting; retry the request")
            self._secure_delete()
            self._apply_atomic(user_id, before, memories)
            self._apply_turns(before, turns)
            self._apply_replacements(user_id, before, memories, turns, new_session)
            self._apply_experiences(user_id, before, experiences)
            self._apply_profile(user_id, before, profiles)
            self._refresh_indexes(user_id, before, memories, turns)
            self._verify_commit(user_id, before, memories, turns, experiences, profiles)
        self.store.invalidate_dense_cache(user_id)
        return ProfileStore(self.store).publish(user_id)

    def _secure_delete(self):
        self.connection.execute("PRAGMA secure_delete = ON")
        if sqlite3.sqlite_version_info >= (3, 42, 0):
            self.connection.execute(
                "INSERT INTO atomic_memories_fts(atomic_memories_fts, rank) "
                "VALUES ('secure-delete', 1)"
            )
            self.connection.execute(
                "INSERT INTO conversation_turns_fts(conversation_turns_fts, rank) "
                "VALUES ('secure-delete', 1)"
            )

    def _apply_atomic(self, user_id, before, memories):
        atomic = AtomicMemoryStore(self.store)
        for memory_id, original in before.memories.items():
            current = memories.get(memory_id)
            if current == original:
                continue
            self.connection.execute("DELETE FROM memory_evidence WHERE memory_id = ?", (memory_id,))
            if current is None:
                self.connection.execute(
                    "DELETE FROM atomic_memories WHERE memory_id = ? AND user_id = ?",
                    (memory_id, user_id),
                )
                continue
            time = current.valid_time or ValidTime()
            self.connection.execute(
                """UPDATE atomic_memories SET content = ?, valid_start = ?, valid_end = ?,
                   valid_point = ?, updated_at = ?, version = version + 1
                   WHERE memory_id = ? AND user_id = ?""",
                (
                    current.content,
                    time.start,
                    time.end,
                    time.point,
                    utc_now().isoformat(),
                    memory_id,
                    user_id,
                ),
            )
            atomic._insert_evidence(memory_id, current.evidence)
            if original.content != current.content or original.valid_time != current.valid_time:
                self.connection.execute(
                    """DELETE FROM memory_embeddings WHERE vector_id IN
                       (SELECT vector_id FROM memory_vector_ids WHERE memory_id = ?)""",
                    (memory_id,),
                )

    def _apply_turns(self, before, turns):
        for turn_id, original in before.turns.items():
            current = turns.get(turn_id)
            if current is None:
                self.connection.execute(
                    "DELETE FROM conversation_turns WHERE turn_id = ?", (turn_id,)
                )
            elif current.content != original.content:
                self.connection.execute(
                    "UPDATE conversation_turns SET content = ? WHERE turn_id = ?",
                    (current.content, turn_id),
                )

    def _apply_replacements(self, user_id, before, memories, turns, new_session):
        self._insert_replacements(user_id, before, memories, turns, new_session)
        # Sanitizing an already extracted source is not a new conversation.
        # Its atomic claims were repaired in this same plan. Preserve the old
        # checkpoint so immediate maintenance does not rewrite those claims and
        # invalidate the derived groups that were just independently verified.
        self.connection.executemany(
            "INSERT OR IGNORE INTO memory_processed_turns(turn_id) VALUES (?)",
            [(key,) for key in sorted(before.processed_turns & turns.keys())],
        )

    def _apply_experiences(self, user_id, before, experiences):
        if experiences is not None:
            experience_store = ExperienceStore(self.store)
            persisted = experience_store.read(
                user_id, set(before.experiences) if before.partial else None
            )
            if before.partial:
                from memory_dm.memory.state import WriteSnapshot

                current = self.read(user_id)
                sources = WriteSnapshot(current.memories, current.turns, set(), 0)
            else:
                sources = MemoryWriteRepository(self.store).read_in_transaction(user_id)
            for key, original in before.experiences.items():
                current = experiences.get(key)
                if current is None or (current.content, current.valid_time) != (
                    original.content,
                    original.valid_time,
                ):
                    experience_store.revoke_in_transaction(
                        original, before.memories, before.experience_cutoff
                    )
                if current is None:
                    self.connection.execute(
                        "DELETE FROM experience_memories WHERE experience_id = ? AND user_id = ?",
                        (key, user_id),
                    )
                elif persisted.get(key) != current:
                    experience_store.write_in_transaction(current, sources)

    def _apply_profile(self, user_id, before, profiles):
        if profiles is not None:
            profile_store = ProfileStore(self.store)
            for key in before.profiles:
                current = profiles.get(key)
                if current is None:
                    self.connection.execute(
                        "DELETE FROM user_profiles WHERE profile_id = ? AND user_id = ?",
                        (key, user_id),
                    )
                elif profile_store.read(user_id).get(key) != current:
                    profile_store.write_in_transaction(current)
            # A successful direct profile edit is already the current view. Do not
            # immediately regenerate it from unchanged lower-level premises.
            profile_store._progress(user_id, profile_store.input_signature(user_id), "completed")

    def _refresh_indexes(self, user_id, before, memories, turns):
        # Remove stale cache copies from earlier ordinary edits, in every embedding space.
        self.connection.execute(
            """DELETE FROM memory_embeddings WHERE vector_id IN (
                   SELECT v.vector_id FROM memory_vector_ids AS v JOIN atomic_memories AS m
                   ON m.memory_id = v.memory_id WHERE m.user_id = ?
                   AND m.content != memory_embeddings.content)""",
            (user_id,),
        )
        self.connection.execute("DELETE FROM dense_snapshots WHERE user_id = ?", (user_id,))
        self.connection.execute(
            """INSERT INTO retrieval_revisions(user_id, revision) VALUES (?, 1)
               ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1""",
            (user_id,),
        )
        # Include deleted IDs, which can no longer be joined to the canonical table.
        ids = list(before.memories.keys() | memories.keys())
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            self.connection.execute(
                f"DELETE FROM atomic_memories_fts WHERE memory_id IN ({placeholders})", chunk
            )
            self.connection.execute(
                f"DELETE FROM fts_pending WHERE memory_id IN ({placeholders})", chunk
            )
        self.connection.execute(
            """DELETE FROM atomic_memories_fts WHERE NOT EXISTS (
               SELECT 1 FROM atomic_memories AS m
               WHERE m.memory_id = atomic_memories_fts.memory_id)"""
        )
        self.connection.execute(
            """INSERT INTO atomic_memories_fts(rowid, memory_id, content)
               SELECT rowid, memory_id, memory_terms(content)
               FROM atomic_memories WHERE user_id = ?""",
            (user_id,),
        )
        self.connection.execute(
            "INSERT INTO atomic_memories_fts(atomic_memories_fts) VALUES ('optimize')"
        )
        RawFTSStore(self.store).apply_pending()
        RawFTSStore(self.store).rebuild_in_transaction(user_id)
        self.connection.execute(
            "INSERT INTO conversation_turns_fts(conversation_turns_fts) VALUES ('optimize')"
        )
        if not RawFTSStore(self.store).verify(user_id, list(before.turns.keys() | turns.keys())):
            raise ForgettingError("Raw keyword index verification failed")

    def _verify_commit(self, user_id, before, memories, turns, experiences, profiles):
        after = self.read(user_id)
        if after.memories != memories or after.turns != turns:
            raise ForgettingError("Stored forgetting result differs from the verified plan")
        if experiences is not None and after.experiences != experiences:
            raise ForgettingError("Stored experience repairs differ from the verified plan")
        if profiles is not None and after.profiles != profiles:
            raise ForgettingError("Stored profile repairs differ from the verified plan")
        expected_processed = (before.processed_turns & turns.keys()) | (
            turns.keys() - before.turns.keys()
        )
        if after.processed_turns != expected_processed:
            raise ForgettingError("Forgetting changed unrelated extraction checkpoints")
        self.verify_indices(user_id, before, memories)

    def _insert_replacements(self, user_id, before, memories, turns, new_session):
        """Insert verified new facts and their sanitized sources inside the same transaction."""
        if new_session is not None:
            if new_session.user_id != user_id:
                raise ForgettingError("Replacement session belongs to another user")
            self.connection.execute(
                "INSERT INTO sessions(session_id, user_id, created_at) VALUES (?, ?, ?)",
                (new_session.session_id, user_id, new_session.created_at.isoformat()),
            )
        for key in turns.keys() - before.turns.keys():
            turn = turns[key]
            if (
                key != turn.turn_id
                or turn.role != "user"
                or self.store.get_session(turn.session_id, user_id) is None
            ):
                raise ForgettingError("Replacement source must be a user turn owned by this user")
            self.connection.execute(
                "INSERT INTO conversation_turns(turn_id, session_id, role, content, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, turn.session_id, turn.role, turn.content, turn.timestamp.isoformat()),
            )
            self.connection.execute(
                "INSERT INTO memory_processed_turns(turn_id) VALUES (?)", (key,)
            )
        atomic = AtomicMemoryStore(self.store)
        for key in memories.keys() - before.memories.keys():
            memory = memories[key]
            if key != memory.id or memory.user_id != user_id or memory.status != "ACTIVE":
                raise ForgettingError("Replacement memory must be active and owned by this user")
            atomic._insert(atomic._validate(memory))

    def verify_indices(self, user_id: str, before: ForgetSnapshot, memories: dict) -> None:
        rows = self.connection.execute(
            """SELECT f.memory_id, f.content FROM atomic_memories_fts AS f
               JOIN atomic_memories AS m ON m.memory_id = f.memory_id WHERE m.user_id = ?""",
            (user_id,),
        ).fetchall()
        expected = {key: index_text(memory.content) for key, memory in memories.items()}
        if (
            len(rows) != len(expected)
            or {row["memory_id"]: row["content"] for row in rows} != expected
        ):
            raise ForgettingError("Keyword index verification failed")
        for memory_id in before.memories.keys() - memories.keys():
            for table in ("memory_evidence", "memory_vector_ids", "atomic_memories_fts"):
                if self.connection.execute(
                    f"SELECT 1 FROM {table} WHERE memory_id = ?", (memory_id,)
                ).fetchone():
                    raise ForgettingError("Deleted memory remains in an index or evidence table")
        if self.connection.execute(
            "SELECT 1 FROM dense_snapshots WHERE user_id = ?", (user_id,)
        ).fetchone():
            raise ForgettingError("An old dense snapshot remains after forgetting")
        for key, current in memories.items():
            original = before.memories.get(key)
            if original is not None and (
                current.content == original.content and current.valid_time == original.valid_time
            ):
                continue
            if self.connection.execute(
                """SELECT 1 FROM memory_embeddings AS e JOIN memory_vector_ids AS v
                   ON v.vector_id = e.vector_id WHERE v.memory_id = ?""",
                (key,),
            ).fetchone():
                raise ForgettingError("An old vector remains for a redacted memory")
