"""Durable embedding records, integer IDs and serialized dense-index snapshots.

This adapter handles SQLite consistency only. FAISS construction, normalization and API
calls stay in the dense index, which supplies already computed vectors and snapshot bytes.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from memory_dm.errors import RetrievalError
from memory_dm.storage.sqlite import SQLiteStore


@dataclass(frozen=True)
class VectorSource:
    memory_id: str
    content: str
    vector_id: int | None
    embedded_content: str | None
    dimension: int | None
    vector: bytes | None


@dataclass(frozen=True)
class EmbeddingRecord:
    memory_id: str
    content: str
    dimension: int
    vector: bytes


@dataclass(frozen=True)
class DenseSnapshot:
    revision: int
    index_data: bytes


@dataclass
class IndexPublication:
    """IDs resolved under the write lock and the locally constructed snapshot to publish."""

    mapping: dict[int, str]
    index_data: bytes | None = None


class VectorRepository:
    def __init__(self, store: SQLiteStore):
        self.store = store
        self.connection = store.connection

    def revision(self, user_id: str) -> int:
        return self.store.memory_revision(user_id)

    def sources(self, user_id: str, embedding_key: str) -> tuple[list[VectorSource], int]:
        revision = self.revision(user_id)
        rows = self.connection.execute(
            """SELECT m.memory_id, m.content, v.vector_id,
                      e.content AS embedded_content, e.dimension, e.vector
               FROM atomic_memories AS m
               LEFT JOIN memory_vector_ids AS v ON v.memory_id = m.memory_id
               LEFT JOIN memory_embeddings AS e ON e.vector_id = v.vector_id AND e.embedding_key = ?
               WHERE m.user_id = ? AND m.status = 'ACTIVE' ORDER BY m.rowid""",
            (embedding_key, user_id),
        ).fetchall()
        if revision != self.revision(user_id):
            raise RetrievalError("Memories changed during indexing; retry the operation")
        return [VectorSource(**dict(row)) for row in rows], revision

    def snapshot_revision(self, user_id: str, embedding_key: str) -> int | None:
        row = self.connection.execute(
            "SELECT revision FROM dense_snapshots WHERE user_id = ? AND embedding_key = ?",
            (user_id, embedding_key),
        ).fetchone()
        return row[0] if row else None

    def snapshot(self, user_id: str, embedding_key: str) -> DenseSnapshot | None:
        row = self.connection.execute(
            """SELECT revision, index_data FROM dense_snapshots
               WHERE user_id = ? AND embedding_key = ?""",
            (user_id, embedding_key),
        ).fetchone()
        return DenseSnapshot(**dict(row)) if row else None

    @contextmanager
    def rebuild_transaction(
        self,
        user_id: str,
        embedding_key: str,
        revision: int,
        sources: list[VectorSource],
        updates: list[EmbeddingRecord],
    ) -> Iterator[IndexPublication]:
        """Allocate IDs, save vectors and publish a snapshot in a single transaction.

        The caller builds/serializes FAISS inside this scope and sets publication.index_data.
        Only local computation belongs in the scope; remote API calls must finish first.
        A failed FAISS build also rolls back vector IDs and embeddings.
        """
        if self.connection.in_transaction:
            raise RetrievalError(
                "Index publication requires a connection without an open transaction"
            )
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            if revision != self.revision(user_id):
                raise RetrievalError("Memories changed during indexing; retry the operation")
            if not sources:
                self.connection.execute(
                    "DELETE FROM dense_snapshots WHERE user_id = ? AND embedding_key = ?",
                    (user_id, embedding_key),
                )
            mapping = self._resolve_ids(sources)
            by_memory = {memory_id: vector_id for vector_id, memory_id in mapping.items()}
            self.connection.executemany(
                """INSERT INTO memory_embeddings
                   (vector_id, embedding_key, content, dimension, vector)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(vector_id, embedding_key) DO UPDATE SET
                   content = excluded.content, dimension = excluded.dimension,
                   vector = excluded.vector""",
                [
                    (
                        by_memory[item.memory_id],
                        embedding_key,
                        item.content,
                        item.dimension,
                        item.vector,
                    )
                    for item in updates
                ],
            )
            publication = IndexPublication(mapping)
            yield publication
            if sources:
                if publication.index_data is None:
                    raise RetrievalError(
                        "An index publication must include its serialized snapshot"
                    )
                self._save_snapshot(user_id, embedding_key, revision, publication.index_data)

    def _resolve_ids(self, sources: list[VectorSource]) -> dict[int, str]:
        mapping = {}
        for source in sources:
            vector_id = source.vector_id
            if vector_id is None:
                # Concurrent indexers do not change the fact revision when assigning IDs.
                # Resolve again under the write lock, including across embedding models.
                assigned = self.connection.execute(
                    "SELECT vector_id FROM memory_vector_ids WHERE memory_id = ?",
                    (source.memory_id,),
                ).fetchone()
                vector_id = (
                    assigned[0]
                    if assigned
                    else self.connection.execute(
                        "INSERT INTO memory_vector_ids(memory_id) VALUES (?)", (source.memory_id,)
                    ).lastrowid
                )
            mapping[vector_id] = source.memory_id
        return mapping

    def _save_snapshot(
        self, user_id: str, embedding_key: str, revision: int, index_data: bytes
    ) -> None:
        if not self.connection.in_transaction:
            raise ValueError("Dense snapshot publication requires an open write transaction")
        self.connection.execute(
            """INSERT INTO dense_snapshots(user_id, embedding_key, revision, index_data)
               VALUES (?, ?, ?, ?) ON CONFLICT(user_id, embedding_key) DO UPDATE SET
               revision = excluded.revision, index_data = excluded.index_data""",
            (user_id, embedding_key, revision, index_data),
        )
