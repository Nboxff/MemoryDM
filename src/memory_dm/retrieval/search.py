"""One-pass dense/sparse retrieval with reciprocal rank fusion."""

from dataclasses import dataclass
from typing import Literal

from memory_dm.llm.clients import EmbeddingClient
from memory_dm.models import AtomicMemory
from memory_dm.retrieval.dense import DenseIndex
from memory_dm.retrieval.filters import SearchFilters
from memory_dm.retrieval.sparse import SparseIndex
from memory_dm.storage.search import SearchRepository
from memory_dm.storage.sqlite import SQLiteStore


def reciprocal_rank_fusion(rankings: list[list[str]], *, k0: int = 60) -> list[tuple[str, float]]:
    if k0 <= 0:
        raise ValueError("RRF constant must be positive")
    scores = {}
    for ranking in rankings:
        for rank, memory_id in enumerate(dict.fromkeys(ranking), 1):
            scores[memory_id] = scores.get(memory_id, 0.0) + 1.0 / (k0 + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


@dataclass(frozen=True)
class SearchResult:
    memory: AtomicMemory
    score: float
    dense_rank: int | None = None
    sparse_rank: int | None = None


class HybridRetriever:
    def __init__(
        self,
        store: SQLiteStore,
        embedding: EmbeddingClient | None = None,
        *,
        batch_size: int | None = None,
    ):
        self.store = store
        self.repository = SearchRepository(store)
        self.sparse = SparseIndex(store)
        self.dense = DenseIndex(store, embedding, batch_size=batch_size) if embedding else None

    def search(
        self,
        query: str,
        user_id: str,
        *,
        mode: Literal["hybrid", "dense", "sparse"] = "hybrid",
        top_k: int = 10,
        dense_top_k: int = 20,
        sparse_top_k: int = 20,
        session_id: str | None = None,
        time_start: str | None = None,
        time_end: str | None = None,
        time_field: Literal["EVENT_TIME", "RECORDED_TIME"] = "EVENT_TIME",
    ) -> list[SearchResult]:
        if not query.strip():
            raise ValueError("Search query must not be blank")
        if mode not in ("hybrid", "dense", "sparse"):
            raise ValueError("Search mode must be hybrid, dense or sparse")
        if any(
            type(value) is not int or value <= 0 for value in (top_k, dense_top_k, sparse_top_k)
        ):
            raise ValueError("Search limits must be positive integers")

        filters = SearchFilters(user_id, session_id, time_start, time_end, time_field)
        eligible = self.repository.eligible_contents(filters)
        if not eligible:
            return []
        dense, sparse = [], []
        if mode in ("hybrid", "dense"):
            if self.dense is None:
                raise ValueError("Dense and hybrid search require configured embeddings")
            dense = self.dense.search(query, user_id, set(eligible), dense_top_k)
        if mode in ("hybrid", "sparse"):
            sparse = self.sparse.search(
                query, filters, sparse_top_k, natural_language=mode == "hybrid"
            )
        dense_ranks = {key: rank for rank, (key, _) in enumerate(dense, 1)}
        sparse_ranks = {key: rank for rank, (key, _) in enumerate(sparse, 1)}
        fused = reciprocal_rank_fusion([[key for key, _ in dense], [key for key, _ in sparse]])
        # Re-read eligibility and content after API calls; stale candidates are never hydrated.
        current = self.repository.current_memories(
            filters, [key for key, _ in fused], expected_contents=eligible, limit=top_k
        )
        results = []
        for memory_id, score in fused:
            memory = current.get(memory_id)
            if memory is None or memory.content != eligible.get(memory_id):
                continue
            results.append(
                SearchResult(
                    memory,
                    score,
                    dense_ranks.get(memory_id),
                    sparse_ranks.get(memory_id),
                )
            )
            if len(results) == top_k:
                break
        return results

    def rebuild(self, user_id: str, *, force_reembed: bool = False) -> dict[str, int]:
        if not user_id.strip():
            raise ValueError("A user ID is required")
        sparse_count = self.sparse.rebuild(user_id)
        if self.dense is None:
            return {"sparse": sparse_count, "dense": 0}
        _, mapping = self.dense.rebuild(user_id, force_reembed=force_reembed)
        return {"sparse": sparse_count, "dense": len(mapping)}
