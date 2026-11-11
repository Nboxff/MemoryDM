"""Experience persistence, incremental work and content-free revocation boundaries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import uuid4

from memory_dm.experience.models import ExperienceMemory, SourceGroup, group_key, validate_group
from memory_dm.memory.state import WriteSnapshot
from memory_dm.models import AtomicMemory, ValidTime, utc_now
from memory_dm.storage.maintenance import MemoryWriteRepository
from memory_dm.storage.sqlite import ConcurrentUpdateError, SQLiteStore


@dataclass(frozen=True)
class Revocation:
    roots: frozenset[str]
    cutoff: int


@dataclass
class ExperienceSnapshot:
    atomic: WriteSnapshot
    experiences: dict[str, ExperienceMemory]
    pending: dict[str, int]
    epochs: dict[str, int]
    revocations: list[Revocation]


def source_fingerprint(memory: AtomicMemory, turns: dict) -> str:
    if any(e.turn_id not in turns for e in memory.evidence):
        raise ValueError("An experience source is missing or belongs to another user")
    sources = [turns[e.turn_id].model_dump(mode="json") for e in memory.evidence]
    value = [memory.model_dump(mode="json"), sources]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def roots(group: SourceGroup, memories: dict[str, AtomicMemory]) -> set[str]:
    return {e.turn_id for key in group.memory_ids for e in memories[key].evidence}


class ExperienceStore:
    def __init__(self, store: SQLiteStore):
        self.store = store
        self.connection = store.connection

    def read(self, user_id: str, ids: set[str] | None = None) -> dict[str, ExperienceMemory]:
        result = {}
        for row in self.connection.execute(
            "SELECT * FROM experience_memories WHERE user_id = ?"
            + (" AND experience_id IN (SELECT value FROM json_each(?))" if ids is not None else "")
            + " ORDER BY experience_id",
            (user_id, json.dumps(sorted(ids))) if ids is not None else (user_id,),
        ).fetchall():
            groups = []
            for group in self.connection.execute(
                "SELECT * FROM experience_groups WHERE experience_id = ? ORDER BY group_id",
                (row["experience_id"],),
            ).fetchall():
                members = [
                    r[0]
                    for r in self.connection.execute(
                        "SELECT memory_id FROM experience_sources WHERE group_id = ? "
                        "ORDER BY memory_id",
                        (group["group_id"],),
                    )
                ]
                groups.append(SourceGroup(basis=group["basis"], memory_ids=members))
            time = ValidTime(
                start=row["valid_start"], end=row["valid_end"], point=row["valid_point"]
            )
            result[row["experience_id"]] = ExperienceMemory(
                id=row["experience_id"],
                user_id=user_id,
                kind=row["kind"],
                content=row["content"],
                valid_time=time if any(time.model_dump().values()) else None,
                evidence_groups=sorted(groups, key=group_key),
                created_at=row["created_at"],
            )
        return result

    def read_in_transaction(self, user_id: str) -> ExperienceSnapshot:
        if not self.connection.in_transaction:
            raise ValueError("Experience snapshot requires a transaction")
        revocations = []
        for row in self.connection.execute(
            "SELECT * FROM experience_revocations WHERE user_id = ? ORDER BY revocation_id",
            (user_id,),
        ).fetchall():
            members = self.connection.execute(
                "SELECT turn_id FROM experience_revoked_sources WHERE revocation_id = ?",
                (row["revocation_id"],),
            ).fetchall()
            revocations.append(Revocation(frozenset(r[0] for r in members), row["cutoff_epoch"]))
        return ExperienceSnapshot(
            MemoryWriteRepository(self.store).read_in_transaction(user_id),
            self.read(user_id),
            dict(
                self.connection.execute(
                    "SELECT memory_id, revision FROM experience_pending WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
            ),
            dict(
                self.connection.execute(
                    "SELECT turn_id, epoch FROM experience_turn_epochs WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
            ),
            revocations,
        )

    def snapshot(self, user_id: str) -> ExperienceSnapshot:
        if not user_id.strip() or self.connection.in_transaction:
            raise ValueError("Experience maintenance requires a user and no open transaction")
        with self.connection:
            self.connection.execute("BEGIN")
            return self.read_in_transaction(user_id)

    def list(self, user_id: str) -> list[ExperienceMemory]:
        before = self.snapshot(user_id)
        for experience in before.experiences.values():
            self._validate_sources(experience, before.atomic)
        return list(before.experiences.values())

    def get(self, experience_id: str, user_id: str) -> ExperienceMemory | None:
        return next((e for e in self.list(user_id) if e.id == experience_id), None)

    def descendants(self, memory_id: str, user_id: str) -> list[ExperienceMemory]:
        ids = {
            row[0]
            for row in self.connection.execute(
                """SELECT DISTINCT e.experience_id FROM experience_memories e
                   JOIN experience_groups g ON g.experience_id = e.experience_id
                   JOIN experience_sources s ON s.group_id = g.group_id
                   JOIN atomic_memories m ON m.memory_id = s.memory_id
                   WHERE s.memory_id = ? AND e.user_id = ? AND m.user_id = ?""",
                (memory_id, user_id, user_id),
            )
        }
        return [e for e in self.list(user_id) if e.id in ids]

    def evidence(self, experience_id: str, user_id: str) -> list[dict]:
        before = self.snapshot(user_id)
        experience = before.experiences.get(experience_id)
        if experience is None:
            raise ValueError("Experience not found for this user")
        self._validate_sources(experience, before.atomic)
        return [
            {
                "basis": group.basis,
                "memories": [
                    before.atomic.memories[key].model_dump(mode="json") for key in group.memory_ids
                ],
                "turns": [
                    before.atomic.turns[key].model_dump(mode="json")
                    for key in sorted(roots(group, before.atomic.memories))
                ],
            }
            for group in experience.evidence_groups
        ]

    @staticmethod
    def _validate_sources(experience: ExperienceMemory, atomic: WriteSnapshot) -> None:
        for group in experience.evidence_groups:
            validate_group(group, atomic.memories, experience.user_id)
            for key in group.memory_ids:
                memory = atomic.memories[key]
                for evidence in memory.evidence:
                    source = atomic.turns.get(evidence.turn_id)
                    if source is None or source.session_id != evidence.session_id:
                        raise ValueError(
                            "Experience source refers to a missing or foreign raw turn"
                        )
                if not any(atomic.turns[e.turn_id].role == "user" for e in memory.evidence):
                    raise ValueError("Experience sources require a supporting user statement")

    def write_in_transaction(self, experience: ExperienceMemory, atomic: WriteSnapshot) -> None:
        if not self.connection.in_transaction:
            raise ValueError("Experience writes require an open transaction")
        self._validate_sources(experience, atomic)
        owner = self.connection.execute(
            "SELECT user_id FROM experience_memories WHERE experience_id = ?", (experience.id,)
        ).fetchone()
        if owner and owner[0] != experience.user_id:
            raise ValueError("An experience ID belongs to a different user")
        time = experience.valid_time or ValidTime()
        self.connection.execute(
            """INSERT INTO experience_memories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(experience_id) DO UPDATE SET kind = excluded.kind,
                 content = excluded.content, valid_start = excluded.valid_start,
                 valid_end = excluded.valid_end, valid_point = excluded.valid_point,
                 updated_at = excluded.updated_at, version = version + 1""",
            (
                experience.id,
                experience.user_id,
                experience.kind,
                experience.content,
                time.start,
                time.end,
                time.point,
                experience.created_at.isoformat(),
                utc_now().isoformat(),
            ),
        )
        self.connection.execute(
            "DELETE FROM experience_groups WHERE experience_id = ?", (experience.id,)
        )
        for group in experience.evidence_groups:
            group_id = str(uuid4())
            self.connection.execute(
                "INSERT INTO experience_groups VALUES (?, ?, ?)",
                (group_id, experience.id, group.basis),
            )
            self.connection.executemany(
                "INSERT INTO experience_sources VALUES (?, ?, ?)",
                [
                    (group_id, key, source_fingerprint(atomic.memories[key], atomic.turns))
                    for key in sorted(group.memory_ids)
                ],
            )

    def save(self, experience: ExperienceMemory) -> None:
        if self.connection.in_transaction:
            raise ValueError("Experience save requires no open transaction")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            before = self.read_in_transaction(experience.user_id)
            self._check_revocations(experience, before)
            self.write_in_transaction(experience, before.atomic)

    def revoke_in_transaction(
        self, experience: ExperienceMemory, memories: dict[str, AtomicMemory], cutoff: int
    ) -> None:
        source_ids = set().union(*(roots(group, memories) for group in experience.evidence_groups))
        self.revoke_roots_in_transaction(experience.user_id, source_ids, cutoff)

    def revoke_roots_in_transaction(self, user_id: str, source_ids: set[str], cutoff: int) -> None:
        if not self.connection.in_transaction:
            raise ValueError("Revocation requires a transaction")
        revocation_id = str(uuid4())
        self.connection.execute(
            "INSERT INTO experience_revocations VALUES (?, ?, ?)",
            (revocation_id, user_id, cutoff),
        )
        self.connection.executemany(
            "INSERT INTO experience_revoked_sources VALUES (?, ?)",
            [(revocation_id, key) for key in sorted(source_ids)],
        )

    def delete(self, experience_id: str, user_id: str) -> bool:
        before = self.snapshot(user_id)
        experience = before.experiences.get(experience_id)
        if experience is None:
            return False
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.read_in_transaction(user_id) != before:
                raise ConcurrentUpdateError("Experience sources changed before deletion")
            self.connection.execute("PRAGMA secure_delete = ON")
            self.revoke_in_transaction(
                experience, before.atomic.memories, max(before.epochs.values(), default=0)
            )
            self.connection.execute(
                "DELETE FROM experience_memories WHERE experience_id = ?", (experience_id,)
            )
        self.store.invalidate_dense_cache(user_id)
        return True

    @staticmethod
    def relearning(group: SourceGroup, before: ExperienceSnapshot) -> list[dict]:
        source_ids = roots(group, before.atomic.memories)
        return ExperienceStore.relearning_roots(source_ids, before)

    @staticmethod
    def relearning_roots(source_ids: set[str], before: ExperienceSnapshot) -> list[dict]:
        checks = []
        for revoked in before.revocations:
            if not source_ids.intersection(revoked.roots):
                continue
            new_sources = [
                key
                for key in source_ids
                if before.epochs.get(key, 0) > revoked.cutoff
                and before.atomic.turns[key].role == "user"
            ]
            checks.append({"new_user_turn_ids": sorted(new_sources)})
        return checks

    def commit(
        self,
        user_id: str,
        before: ExperienceSnapshot,
        experiences: list[ExperienceMemory],
        processed: list[str],
    ) -> None:
        if self.connection.in_transaction:
            raise ValueError("Experience commit requires no open transaction")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.read_in_transaction(user_id) != before:
                raise ConcurrentUpdateError(
                    "Memory or experience changed during consolidation; retry"
                )
            if not set(processed).issubset(before.pending):
                raise ValueError("Only pending sources may be checkpointed")
            for experience in experiences:
                if experience.user_id != user_id:
                    raise ValueError("Experience belongs to another user")
                self._check_revocations(experience, before)
                self.write_in_transaction(experience, before.atomic)
            self.connection.executemany(
                "DELETE FROM experience_pending WHERE user_id = ? AND memory_id = ?",
                [(user_id, key) for key in processed],
            )
            self.connection.execute("DELETE FROM experience_failures WHERE user_id = ?", (user_id,))

    def _check_revocations(self, experience: ExperienceMemory, before: ExperienceSnapshot) -> None:
        old = before.experiences.get(experience.id)
        preserved = (
            {group_key(g) for g in old.evidence_groups}
            if old
            and (old.content, old.kind, old.valid_time)
            == (experience.content, experience.kind, experience.valid_time)
            else set()
        )
        for group in experience.evidence_groups:
            validate_group(group, before.atomic.memories, experience.user_id)
            if group_key(group) not in preserved and any(
                not check["new_user_turn_ids"] for check in self.relearning(group, before)
            ):
                raise ValueError("A revoked derivation requires new user evidence")

    @staticmethod
    def work_key(before: ExperienceSnapshot, memory_ids=None) -> str:
        items = [
            (
                key,
                revision,
                source_fingerprint(before.atomic.memories[key], before.atomic.turns)
                if key in before.atomic.memories
                else None,
            )
            for key, revision in sorted(before.pending.items())
            if memory_ids is None or key in memory_ids
        ]
        return hashlib.sha256(json.dumps(items).encode()).hexdigest()

    def failure_attempts(self, user_id: str, input_hash: str) -> int:
        row = self.connection.execute(
            "SELECT input_hash, attempts FROM experience_failures WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row[1] if row and row[0] == input_hash else 0

    def record_failure(self, user_id: str, input_hash: str, code: str, *, memory_ids=None) -> None:
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            current = self.work_key(self.read_in_transaction(user_id), memory_ids)
            self.connection.execute(
                """INSERT INTO experience_failures VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET input_hash = excluded.input_hash,
                     attempts = CASE WHEN input_hash = excluded.input_hash
                                     THEN attempts + excluded.attempts ELSE excluded.attempts END,
                     error_code = excluded.error_code""",
                (user_id, current, int(current == input_hash), code),
            )

    def schedule(self, user_id: str, memory_ids: list[str]) -> None:
        """Explicitly recheck current sources, without bypassing revocation or validation."""
        before = self.snapshot(user_id)
        if any(key not in before.atomic.memories for key in memory_ids):
            raise ValueError("Scheduled sources must belong to the current user")
        with self.connection:
            self.connection.executemany(
                """INSERT INTO experience_pending VALUES (?, ?, 1)
                   ON CONFLICT(user_id, memory_id) DO UPDATE SET revision = revision + 1""",
                [(user_id, key) for key in memory_ids],
            )
