"""One transactional document and one replaceable Markdown file per user.

SQLite owns the committed document so forgetting remains atomic across layers. The
file is a materialized view, repaired on opening a store after an interrupted write.
No profile fragments, source mappings or historical body copies are retained.
"""

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from memory_dm.experience.models import ExperienceMemory
from memory_dm.models import AtomicMemory
from memory_dm.profile.models import UserProfile
from memory_dm.storage.atomic import AtomicMemoryStore
from memory_dm.storage.experience import ExperienceStore
from memory_dm.storage.sqlite import ConcurrentUpdateError, SQLiteStore


@dataclass
class ProfileSnapshot:
    memories: dict[str, AtomicMemory]
    experiences: dict[str, ExperienceMemory]
    document: UserProfile | None
    signature: str


class ProfileStore:
    def __init__(self, store: SQLiteStore):
        self.store, self.connection = store, store.connection

    def get(self, user_id: str) -> UserProfile | None:
        row = self.connection.execute(
            "SELECT * FROM user_profiles WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        item = UserProfile(
            user_id=user_id,
            id=row["profile_id"],
            content=row["content"],
            created_at=row["created_at"],
        )
        return item

    def read(self, user_id: str) -> dict[str, UserProfile]:
        """Expose the single document to the common layer retrieval/forgetting interface."""
        document = self.get(user_id)
        return {document.id: document} if document else {}

    def snapshot_in_transaction(self, user_id: str) -> ProfileSnapshot:
        memories = {
            m.id: m for m in AtomicMemoryStore(self.store).list(user_id) if m.status == "ACTIVE"
        }
        experiences = ExperienceStore(self.store).read(user_id)
        document = self.get(user_id)
        signature = self.input_signature(user_id)
        return ProfileSnapshot(memories, experiences, document, signature)

    def input_signature(self, user_id: str) -> str:
        # Profile maintenance depends on atomic/experience versions and the document,
        # not raw dialogue text. This checkpoint needs no full content hydration.
        atomic = [
            tuple(r)
            for r in self.connection.execute(
                "SELECT memory_id, version FROM atomic_memories "
                "WHERE user_id=? AND status='ACTIVE' "
                "ORDER BY memory_id",
                (user_id,),
            )
        ]
        experiences = [
            tuple(r)
            for r in self.connection.execute(
                "SELECT experience_id, version FROM experience_memories WHERE user_id=? "
                "ORDER BY experience_id",
                (user_id,),
            )
        ]
        profile = self.get(user_id)
        value = [atomic, experiences, profile.model_dump(mode="json") if profile else None]
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def snapshot(self, user_id: str) -> ProfileSnapshot:
        if self.connection.in_transaction:
            raise ValueError("Profile snapshot requires no open transaction")
        with self.connection:
            self.connection.execute("BEGIN")
            return self.snapshot_in_transaction(user_id)

    def write_in_transaction(self, profile: UserProfile) -> None:
        profile = UserProfile.model_validate(profile.model_dump())
        self.connection.execute(
            "INSERT INTO user_profiles VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE "
            "SET content=excluded.content, profile_id=excluded.profile_id, "
            "created_at=excluded.created_at",
            (profile.user_id, profile.id, profile.content, profile.created_at.isoformat()),
        )

    def commit(self, user_id: str, before: ProfileSnapshot, document: UserProfile | None) -> bool:
        if document is not None and document.user_id != user_id:
            raise ValueError("A profile document belongs to another user")
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if self.snapshot_in_transaction(user_id) != before:
                raise ConcurrentUpdateError("Profile inputs changed during consolidation; retry")
            if document is not None and document != before.document:
                self.write_in_transaction(document)
            elif document is None and before.document is not None:
                self.connection.execute("DELETE FROM user_profiles WHERE user_id=?", (user_id,))
            current = self.snapshot_in_transaction(user_id)
            self._progress(user_id, current.signature, "completed")
        return self.publish(user_id)

    def _progress(self, user_id: str, signature: str, status: str) -> None:
        self.connection.execute(
            "INSERT INTO profile_progress VALUES (?, ?, 1, ?) ON CONFLICT(user_id) DO UPDATE "
            "SET attempts=CASE WHEN input_hash=excluded.input_hash THEN attempts+1 ELSE 1 END, "
            "input_hash=excluded.input_hash, status=excluded.status",
            (user_id, signature, status),
        )

    def markdown(self, user_id: str) -> str:
        document = self.get(user_id)
        return document.content if document else ""

    def file_path(self, user_id: str) -> Path | None:
        row = self.connection.execute("PRAGMA database_list").fetchone()
        if not row[2]:
            return None
        database = Path(row[2])
        owner = hashlib.sha256(user_id.encode()).hexdigest()
        return database.parent / (database.name + ".profiles") / (owner + ".md")

    def sync_file(self, user_id: str) -> None:
        if self.connection.in_transaction:
            raise ValueError("Publish a profile file only after committing its transaction")
        path = self.file_path(user_id)
        if path is None:
            return
        # Serialize publication with other connections to this database.
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            # Every publisher holds this lock. Any old temporary file is therefore
            # abandoned, and must not keep a superseded or forgotten document body.
            if path.parent.exists():
                for abandoned in path.parent.glob(".profile-*"):
                    abandoned.unlink(missing_ok=True)
            text = self.markdown(user_id)
            if not text:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=path.parent, prefix=".profile-", delete=False
                ) as output:
                    temporary = Path(output.name)
                    output.write(text)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)

    def publish(self, user_id: str) -> bool:
        """An I/O failure after DB commit must not be reported as a rolled-back write."""
        try:
            self.sync_file(user_id)
        except (OSError, sqlite3.Error):
            return False
        return True
