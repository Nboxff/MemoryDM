"""Cosine retrieval using durable integer IDs and rebuildable FAISS snapshots."""

import json

import faiss
import numpy as np

from memory_dm.errors import RetrievalError as RetrievalError
from memory_dm.llm.clients import ClientError, EmbeddingClient
from memory_dm.storage.sqlite import SQLiteStore
from memory_dm.storage.vectors import EmbeddingRecord, VectorRepository


def normalized(vectors: list[list[float]] | np.ndarray) -> np.ndarray:
    with np.errstate(over="ignore", invalid="ignore"):
        matrix = np.asarray(vectors, dtype="float32")
    if matrix.ndim != 2 or matrix.shape[1] == 0 or not np.isfinite(matrix).all():
        raise ClientError("Embedding vectors must be finite and have consistent dimensions")
    norms = np.linalg.norm(matrix.astype("float64"), axis=1)
    if np.any(norms == 0):
        raise ClientError("Embedding vectors must be non-zero")
    return np.ascontiguousarray(matrix / norms[:, None], dtype="float32")


class DenseIndex:
    # Account for vector payloads plus IDs; Python/container overhead is additional.
    CACHE_BYTES = 128 * 1024 * 1024
    CACHE_ENTRIES = 8

    def __init__(
        self, store: SQLiteStore, client: EmbeddingClient, *, batch_size: int | None = None
    ):
        self.repository = VectorRepository(store)
        self.cache = store.dense_index_cache
        self.client = client
        self.batch_size = batch_size
        self.key = json.dumps([client.config.base_url.rstrip("/"), client.config.model])

    def _remember(self, user_id, revision, index, mapping):
        key = (user_id, self.key)
        self.cache.pop(key, None)
        if index is None:
            return
        size = index.ntotal * (index.d * 4 + 8)
        if size > self.CACHE_BYTES:
            return
        self.cache[key] = (revision, index, mapping, size)
        while (
            len(self.cache) > self.CACHE_ENTRIES
            or sum(entry[3] for entry in self.cache.values()) > self.CACHE_BYTES
        ):
            self.cache.popitem(last=False)

    def _revision(self, user_id: str) -> int:
        return self.repository.revision(user_id)

    def load(self, user_id: str):
        key = (user_id, self.key)
        cached = self.cache.get(key)
        if cached is not None:
            # Revision catches ordinary SQLite writers; snapshot presence also catches direct
            # vector-cache edits, whose triggers invalidate the persisted snapshot.
            snapshot_revision = self.repository.snapshot_revision(user_id, self.key)
            if snapshot_revision is not None and snapshot_revision == cached[0] == self._revision(
                user_id
            ):
                self.cache.move_to_end(key)
                return cached[1], cached[2]
            self.cache.pop(key, None)
        rows, revision = self.repository.sources(user_id, self.key)
        snapshot = self.repository.snapshot(user_id, self.key)
        if snapshot and snapshot.revision == revision:
            try:
                if len(snapshot.index_data) < 32:
                    raise ValueError("Truncated index")
                index = faiss.deserialize_index(np.frombuffer(snapshot.index_data, dtype="uint8"))
                mapping = {row.vector_id: row.memory_id for row in rows}
                if (
                    isinstance(index, faiss.IndexIDMap2)
                    and index.metric_type == faiss.METRIC_INNER_PRODUCT
                    and index.ntotal == len(rows)
                    and set(faiss.vector_to_array(index.id_map).tolist()) == set(mapping)
                    and all(
                        row.embedded_content == row.content and row.dimension == index.d
                        for row in rows
                    )
                ):
                    index.check_consistency()
                    self._remember(user_id, revision, index, mapping)
                    return index, mapping
            except (RuntimeError, ValueError):
                pass
        return self.rebuild(user_id)

    def rebuild(self, user_id: str, *, force_reembed: bool = False):
        rows, revision = self.repository.sources(user_id, self.key)
        vectors = {}
        missing = []
        for row in rows:
            if not force_reembed and row.embedded_content == row.content:
                try:
                    vector = np.frombuffer(row.vector, dtype="<f4")
                    if len(vector) == row.dimension:
                        vectors[row.memory_id] = normalized(vector[None, :])[0]
                        continue
                except (ValueError, TypeError, ClientError):
                    pass
            missing.append(row)
        if missing:
            fresh = self.client.embed_batch(
                [row.content for row in missing], batch_size=self.batch_size
            )
            matrix = normalized(fresh)
            vectors.update(
                {row.memory_id: vector for row, vector in zip(missing, matrix, strict=True)}
            )
        dimensions = {len(vector) for vector in vectors.values()}
        if len(dimensions) > 1:
            raise RetrievalError("Embedding dimension changed; rebuild with force_reembed=True")
        updates = [
            EmbeddingRecord(
                row.memory_id,
                row.content,
                len(vectors[row.memory_id]),
                vectors[row.memory_id].astype("<f4").tobytes(),
            )
            for row in missing
        ]
        # HTTP work is complete. The repository owns the lock, revision check and durable IDs;
        # FAISS construction stays here, and a failure rolls back the whole publication.
        with self.repository.rebuild_transaction(
            user_id, self.key, revision, rows, updates
        ) as publication:
            mapping = publication.mapping
            if not rows:
                self._remember(user_id, revision, None, {})
                return None, {}
            index = faiss.IndexIDMap2(faiss.IndexFlatIP(next(iter(dimensions))))
            index.add_with_ids(
                np.asarray([vectors[memory_id] for memory_id in mapping.values()], dtype="float32"),
                np.asarray(list(mapping), dtype="int64"),
            )
            publication.index_data = faiss.serialize_index(index).tobytes()
        self._remember(user_id, revision, index, mapping)
        return index, mapping

    def search(
        self, query: str, user_id: str, eligible: set[str], top_k: int
    ) -> list[tuple[str, float]]:
        index, mapping = self.load(user_id)
        if index is None:
            return []
        selected = {key: value for key, value in mapping.items() if value in eligible}
        if not selected:
            return []
        parameters = None
        if len(selected) != len(mapping):
            # IDMap2 translates external IDs for the selector. Filter inside FAISS before top-k,
            # without reconstructing and copying a temporary index for every time/session filter.
            selector = faiss.IDSelectorBatch(np.asarray(list(selected), dtype="int64"))
            parameters = faiss.SearchParameters(sel=selector)
        vector = normalized([self.client.embed(query)])
        if vector.shape[1] != index.d:
            raise RetrievalError(
                "Query embedding dimension changed; run rebuild-index --force-reembed"
            )
        scores, labels = index.search(vector, min(top_k, len(selected)), params=parameters)
        return [
            (selected[int(key)], float(score))
            for key, score in zip(labels[0], scores[0], strict=True)
            if key >= 0 and np.isfinite(score)
        ]
