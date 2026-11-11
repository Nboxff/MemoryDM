"""Bounded candidate retrieval; no full-content snapshots or cold dense-index rebuilds."""

import json

import numpy as np

from memory_dm.forgetting.models import ForgettingError
from memory_dm.llm.clients import ClientError
from memory_dm.retrieval.dense import normalized
from memory_dm.storage.fts import FTSIndexStore
from memory_dm.storage.indexing import match_query
from memory_dm.storage.raw_fts import RawFTSStore


class ForgetDiscovery:
    def __init__(self, store, embedding=None, *, limit=128):
        self.store, self.embedding, self.limit = store, embedding, limit
        self.queries = {}
        self.exhausted = True

    def search(self, spec, user_id, *, seen=()):
        """Fetch a fresh page; staged records are excluded and verified separately.

        Query expansion locates paraphrases in RAW and all atomic statuses. Existing
        atomic vectors add semantic candidates without embedding the user's entire store.
        Time fields are searched separately, including dates absent from content.
        """
        FTSIndexStore(self.store).synchronize()
        RawFTSStore(self.store).synchronize()
        seen = set(seen)
        result = set()
        self.exhausted = True
        queries = list(dict.fromkeys([spec.target_description, *spec.search_queries]))
        for layer, table, key, owner, join, fts in (
            (
                "RAW",
                "conversation_turns",
                "turn_id",
                "s.user_id",
                "JOIN sessions s ON s.session_id=t.session_id",
                "conversation_turns_fts",
            ),
            ("ATOMIC", "atomic_memories", "memory_id", "t.user_id", "", "atomic_memories_fts"),
        ):
            excluded = json.dumps(sorted(k for layer_name, k in seen if layer_name == layer))
            base = f"{owner}=? AND t.{key} NOT IN (SELECT value FROM json_each(?))"
            terms = [match_query(q, natural_language=True) for q in queries]
            if spec.match_all:
                terms = [None]
            for expression in dict.fromkeys(terms):
                if expression == "":
                    continue
                sql = f"SELECT t.{key} FROM {table} t {join} "
                params = [user_id, excluded]
                if expression is not None:
                    sql += f"JOIN {fts} f ON f.{key}=t.{key} "
                sql += f"WHERE {base}"
                if expression is not None:
                    sql += f" AND {fts} MATCH ?"
                    params.append(expression)
                sql += f" ORDER BY t.{key} LIMIT ?"
                rows = self.store.connection.execute(sql, [*params, self.limit + 1]).fetchall()
                self.exhausted &= len(rows) <= self.limit
                result.update((layer, row[0]) for row in rows[: self.limit])
            if spec.time_start or spec.time_end:
                from memory_dm.models import date_bounds

                lo = (
                    date_bounds(spec.time_start)[0].isoformat() if spec.time_start else "0001-01-01"
                )
                hi = date_bounds(spec.time_end)[1].isoformat() if spec.time_end else "9999-12-31"
                if layer == "RAW":
                    condition = "date(t.timestamp) BETWEEN ? AND ?"
                    bounds = [lo, hi]
                    # RAW event dates are free text: semantic/keyword queries supply
                    # candidates; recording timestamps must not stand in for event time.
                    if spec.time_field == "EVENT_TIME":
                        continue
                elif spec.time_field == "RECORDED_TIME":
                    condition = "date(t.created_at) BETWEEN ? AND ?"
                    bounds = [lo, hi]
                else:
                    condition = (
                        "COALESCE(memory_date_start(t.valid_point), "
                        "memory_date_start(t.valid_start), '0001-01-01') <= ? AND "
                        "COALESCE(memory_date_end(t.valid_point), "
                        "memory_date_end(t.valid_end), '9999-12-31') >= ? AND "
                        "(t.valid_point IS NOT NULL OR t.valid_start IS NOT NULL OR "
                        "t.valid_end IS NOT NULL)"
                    )
                    bounds = [hi, lo]
                rows = self.store.connection.execute(
                    f"SELECT t.{key} FROM {table} t {join} WHERE {base} AND {condition} "
                    f"ORDER BY t.{key} LIMIT ?",
                    [user_id, excluded, *bounds, self.limit + 1],
                ).fetchall()
                self.exhausted &= len(rows) <= self.limit
                result.update((layer, r[0]) for r in rows[: self.limit])
        if self.embedding is not None and not spec.match_all:
            result.update(self._dense(queries, user_id, seen))
        from memory_dm.retrieval.derived import DerivedRetriever
        from memory_dm.retrieval.filters import SearchFilters
        from memory_dm.retrieval.models import SearchTask

        derived = DerivedRetriever(self.store, self.embedding)
        graph = derived.graph(user_id)
        graph.nodes = {
            key: node
            for key, node in graph.nodes.items()
            if key[0] != "EXPERIENCE" or key not in seen
        }
        if spec.match_all:
            result.update(key for key in graph.nodes if key[0] == "EXPERIENCE")
        else:
            for query in queries:
                try:
                    hits = derived.hybrid(
                        graph,
                        SearchTask(
                            query=query,
                            layers=["EXPERIENCE"],
                            top_k=50,
                            dense_top_k=50,
                            sparse_top_k=50,
                        ).model_copy(
                            update={
                                "top_k": self.limit,
                                "dense_top_k": self.limit,
                                "sparse_top_k": self.limit,
                            }
                        ),
                        SearchFilters(user_id),
                        "hybrid" if self.embedding else "sparse",
                    )
                except (ClientError, ValueError) as error:
                    raise ForgettingError("Experience candidate retrieval failed") from error
                result.update((hit.layer, hit.id) for hit in hits)
                self.exhausted &= len(hits) < self.limit
        return result

    def _dense(self, queries, user_id, seen):
        key = json.dumps([self.embedding.config.base_url.rstrip("/"), self.embedding.config.model])
        rows = self.store.connection.execute(
            "SELECT m.memory_id, e.vector, e.dimension FROM memory_embeddings e "
            "JOIN memory_vector_ids v ON v.vector_id=e.vector_id "
            "JOIN atomic_memories m ON m.memory_id=v.memory_id "
            "WHERE m.user_id=? AND e.embedding_key=? AND e.content=m.content "
            "AND m.memory_id NOT IN (SELECT value FROM json_each(?))",
            (
                user_id,
                key,
                json.dumps(sorted(k for layer_name, k in seen if layer_name == "ATOMIC")),
            ),
        ).fetchall()
        if not rows:
            return set()
        result = set()
        try:
            for query in queries:
                if query not in self.queries:
                    self.queries[query] = normalized(self.embedding.embed_batch([query]))[0]
                vector = self.queries[query]
                ranked = []
                for row in rows:
                    candidate = np.frombuffer(row["vector"], dtype="<f4")
                    if len(candidate) != len(vector):
                        continue
                    score = float(normalized(candidate[None, :])[0] @ vector)
                    if score >= 0.3:
                        ranked.append((score, row["memory_id"]))
                ranked.sort(reverse=True)
                self.exhausted &= len(ranked) <= self.limit
                result.update(("ATOMIC", k) for _, k in ranked[: self.limit])
        except (ClientError, ValueError) as error:
            # Callers retain other repairs but must report incomplete discovery.
            raise ForgettingError("Semantic candidate retrieval failed") from error
        return result
