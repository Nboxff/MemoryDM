"""Read and commit a growing, explicitly selected forgetting working set."""

import json

from memory_dm.forgetting.models import ForgettingError
from memory_dm.forgetting.storage import ForgetRepository, ForgetSnapshot
from memory_dm.storage.atomic import AtomicMemoryStore
from memory_dm.storage.experience import ExperienceStore
from memory_dm.storage.fts import FTSIndexStore
from memory_dm.storage.indexing import index_text
from memory_dm.storage.profile import ProfileStore
from memory_dm.storage.raw_fts import RawFTSStore


def encoded(values):
    return json.dumps(sorted(values))


class ScopedForgetRepository(ForgetRepository):
    def __init__(self, store):
        super().__init__(store)
        self.reset()

    def reset(self):
        self.atom_ids, self.raw_ids, self.experience_ids = set(), set(), set()
        self.profile_loaded = False

    def signature(self, user_id):
        # Canonical revisions guard unseen inserts as well as selected content edits.
        # Derived dependency changes are checked without reading their content.
        dependencies = tuple(
            tuple(r)
            for r in self.connection.execute(
                "SELECT e.experience_id, e.version, g.group_id, s.memory_id, s.fingerprint "
                "FROM experience_memories e LEFT JOIN experience_groups g "
                "ON g.experience_id=e.experience_id LEFT JOIN experience_sources s "
                "ON s.group_id=g.group_id WHERE e.user_id=? "
                "ORDER BY e.experience_id, g.group_id, s.memory_id",
                (user_id,),
            )
        )
        processed = tuple(
            r[0]
            for r in self.connection.execute(
                "SELECT p.turn_id FROM memory_processed_turns p JOIN conversation_turns t "
                "ON t.turn_id=p.turn_id JOIN sessions s ON s.session_id=t.session_id "
                "WHERE s.user_id=? ORDER BY p.turn_id",
                (user_id,),
            )
        )
        return self.store.data_revision(user_id), dependencies, processed

    def read(self, user_id):
        atomic = AtomicMemoryStore(self.store)
        memories = {
            key: item
            for key in sorted(self.atom_ids)
            if (item := atomic.get(key, user_id)) is not None
        }
        from memory_dm.models import Turn

        turns = {
            r["turn_id"]: Turn.model_validate(dict(r))
            for r in self.connection.execute(
                "SELECT t.* FROM conversation_turns t JOIN sessions s ON s.session_id=t.session_id "
                "WHERE s.user_id=? AND t.turn_id IN (SELECT value FROM json_each(?)) "
                "ORDER BY t.rowid",
                (user_id, encoded(self.raw_ids)),
            )
        }
        if any(e.turn_id not in turns for m in memories.values() for e in m.evidence):
            raise ForgettingError("Selected memory has a missing or foreign source")
        signature = self.signature(user_id)
        cutoff = self.connection.execute(
            "SELECT COALESCE(MAX(epoch),0) FROM experience_turn_epochs WHERE user_id=?",
            (user_id,),
        ).fetchone()[0]
        return ForgetSnapshot(
            memories,
            turns,
            signature,
            ExperienceStore(self.store).read(user_id, self.experience_ids),
            cutoff,
            ProfileStore(self.store).read(user_id) if self.profile_loaded else {},
            set(signature[2]) & turns.keys(),
            partial=True,
        )

    def expand(self, user_id, before, state, candidates):
        """Complete sources of retrieved atoms and descendants of candidate RAW.

        Supporting sources for unrelated alternative experience groups are loaded only
        for evidence checking, not promoted to deletion candidates.
        """
        if self.signature(user_id) != before.signature:
            raise ForgettingError("User data changed during forgetting; retry the request")
        self.experience_ids.update(k for layer, k in candidates if layer == "EXPERIENCE")
        raw = {k for layer, k in candidates if layer == "RAW"}
        atoms = {k for layer, k in candidates if layer == "ATOMIC"}
        atomic = AtomicMemoryStore(self.store)
        for key in atoms:
            item = atomic.get(key, user_id)
            if item:
                raw.update(e.turn_id for e in item.evidence)
        atoms.update(
            r[0]
            for r in self.connection.execute(
                "SELECT DISTINCT e.memory_id FROM memory_evidence e JOIN atomic_memories m "
                "ON m.memory_id=e.memory_id WHERE m.user_id=? "
                "AND e.turn_id IN (SELECT value FROM json_each(?))",
                (user_id, encoded(raw)),
            )
        )
        self.atom_ids.update(atoms)
        self.raw_ids.update(raw)
        # All atomic sources must be available for evidence repair and support checks.
        for key in self.atom_ids - before.memories.keys():
            item = atomic.get(key, user_id)
            if item:
                self.raw_ids.update(e.turn_id for e in item.evidence)
        for key in raw:
            for order, op in (("DESC", "<"), ("ASC", ">")):
                self.raw_ids.update(
                    r[0]
                    for r in self.connection.execute(
                        "SELECT t.turn_id FROM conversation_turns t JOIN conversation_turns anchor "
                        "ON t.session_id=anchor.session_id "
                        "JOIN sessions s ON s.session_id=t.session_id "
                        f"WHERE anchor.turn_id=? AND s.user_id=? AND t.rowid {op} anchor.rowid "
                        f"ORDER BY t.rowid {order} LIMIT 2",
                        (key, user_id),
                    )
                )
        current = self.read(user_id)
        if current.signature != before.signature:
            raise ForgettingError("User data changed during forgetting; retry the request")
        self._merge(before, state, current)
        return (
            {("RAW", k) for k in raw}
            | {("ATOMIC", k) for k in atoms}
            | {(layer, key) for layer, key in candidates if layer == "EXPERIENCE"}
        )

    @staticmethod
    def _merge(before, state, current):
        for original, staged, fresh in (
            (before.memories, state.memories, current.memories),
            (before.turns, state.turns, current.turns),
        ):
            for key, value in fresh.items():
                if key not in original:
                    original[key] = staged[key] = value
        before.turns = {key: before.turns[key] for key in current.turns}
        before.processed_turns.update(current.processed_turns)
        before.experiences.update(current.experiences)
        before.profiles.update(current.profiles)

    def load_experiences(self, user_id, before, state, changed_atoms):
        self.experience_ids.update(
            r[0]
            for r in self.connection.execute(
                "SELECT DISTINCT e.experience_id FROM experience_memories e "
                "JOIN experience_groups g ON g.experience_id=e.experience_id "
                "JOIN experience_sources s ON s.group_id=g.group_id WHERE e.user_id=? "
                "AND s.memory_id IN (SELECT value FROM json_each(?))",
                (user_id, encoded(changed_atoms)),
            )
        )
        experiences = ExperienceStore(self.store).read(user_id, self.experience_ids)
        support_ids = {
            k for e in experiences.values() for g in e.evidence_groups for k in g.memory_ids
        }
        atomic = AtomicMemoryStore(self.store)
        for key in support_ids:
            item = atomic.get(key, user_id)
            if item:
                self.atom_ids.add(key)
                self.raw_ids.update(e.turn_id for e in item.evidence)
        current = self.read(user_id)
        if current.signature != before.signature:
            raise ForgettingError("User data changed during forgetting; retry the request")
        self._merge(before, state, current)

    def apply(self, user_id, before, memories, turns, **kwargs):
        # New replacement records belong to the explicit working set too.
        self.atom_ids.update(memories)
        self.raw_ids.update(turns)
        return super().apply(user_id, before, memories, turns, **kwargs)

    def _refresh_indexes(self, user_id, before, memories, turns):
        self.connection.execute(
            "DELETE FROM memory_embeddings WHERE vector_id IN "
            "(SELECT v.vector_id FROM memory_vector_ids v JOIN atomic_memories m "
            "ON m.memory_id=v.memory_id WHERE m.user_id=? "
            "AND m.content!=memory_embeddings.content)",
            (user_id,),
        )
        self.connection.execute("DELETE FROM dense_snapshots WHERE user_id=?", (user_id,))
        self.connection.execute(
            "INSERT INTO retrieval_revisions(user_id,revision) VALUES (?,1) "
            "ON CONFLICT(user_id) DO UPDATE SET revision=revision+1",
            (user_id,),
        )
        FTSIndexStore(self.store).apply_pending()
        RawFTSStore(self.store).apply_pending()
        # FTS optimize merges old segments after DELETE/UPDATE; no full rebuild needed.
        for table in ("atomic_memories_fts", "conversation_turns_fts"):
            self.connection.execute(f"INSERT INTO {table}({table}) VALUES ('optimize')")
        ids = before.turns.keys() | turns.keys()
        rows = self.connection.execute(
            "SELECT turn_id, content FROM conversation_turns_fts "
            "WHERE turn_id IN (SELECT value FROM json_each(?))",
            (encoded(ids),),
        ).fetchall()
        if len(rows) != len(turns) or {r[0]: r[1] for r in rows} != {
            k: index_text(t.content) for k, t in turns.items()
        }:
            raise ForgettingError("Raw keyword index verification failed")

    def verify_indices(self, user_id, before, memories):
        ids = before.memories.keys() | memories.keys()
        rows = self.connection.execute(
            "SELECT memory_id, content FROM atomic_memories_fts "
            "WHERE memory_id IN (SELECT value FROM json_each(?))",
            (encoded(ids),),
        ).fetchall()
        if len(rows) != len(memories) or {r[0]: r[1] for r in rows} != {
            k: index_text(m.content) for k, m in memories.items()
        }:
            raise ForgettingError("Keyword index verification failed")
        for key in before.memories.keys() - memories.keys():
            for table in ("memory_evidence", "memory_vector_ids", "atomic_memories_fts"):
                if self.connection.execute(
                    f"SELECT 1 FROM {table} WHERE memory_id=?", (key,)
                ).fetchone():
                    raise ForgettingError("Deleted memory remains in an index or evidence table")
        if self.connection.execute(
            "SELECT 1 FROM dense_snapshots WHERE user_id=?", (user_id,)
        ).fetchone():
            raise ForgettingError("An old dense snapshot remains after forgetting")
        for key, current in memories.items():
            original = before.memories.get(key)
            if original and (original.content, original.valid_time) == (
                current.content,
                current.valid_time,
            ):
                continue
            if self.connection.execute(
                "SELECT 1 FROM memory_embeddings e JOIN memory_vector_ids v "
                "ON e.vector_id=v.vector_id WHERE v.memory_id=?",
                (key,),
            ).fetchone():
                raise ForgettingError("An old vector remains for a redacted memory")
