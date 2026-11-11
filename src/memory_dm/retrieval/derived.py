"""Hybrid and agentic grep retrieval over supported derived claims.

Derived vectors are connection-local and bounded. No additional text/vector copies
are persisted, so source invalidation removes their canonical retrieval surface.
"""

import math
import time
from collections import Counter
from hashlib import sha256

import regex

from memory_dm.errors import RetrievalError
from memory_dm.retrieval.evidence import RetrievedEvidence
from memory_dm.retrieval.search import reciprocal_rank_fusion
from memory_dm.storage.indexing import query_terms, search_terms
from memory_dm.storage.provenance import ProvenanceGraph
from memory_dm.storage.search import SearchRepository


class DerivedRetriever:
    CACHE_BYTES = 32 * 1024 * 1024
    CACHE_ENTRIES = 4

    def __init__(self, store, embedding=None):
        self.store, self.embedding = store, embedding
        self.repository = SearchRepository(store)

    def eligible(self, graph, filters, layers):
        atoms = set(self.repository.eligible_contents(filters))
        return {
            k: n
            for k, n in graph.nodes.items()
            if k[0] in set(layers) & {"EXPERIENCE", "PROFILE"}
            and graph.supported(k, atoms)
            and (
                k[0] != "PROFILE"
                or not (filters.session_id or filters.time_start or filters.time_end)
            )
        }

    @staticmethod
    def _lexical(query, nodes):
        terms = set(query_terms(query))
        documents = {key: Counter(search_terms(node.content)) for key, node in nodes.items()}
        average = sum(sum(words.values()) for words in documents.values()) / max(1, len(nodes))
        frequencies = Counter(term for words in documents.values() for term in words)
        scored = []
        for key, words in documents.items():
            length = sum(words.values())
            score = 0.0
            for term in terms.intersection(words):
                df = frequencies[term]
                idf = math.log(1 + (len(nodes) - df + 0.5) / (df + 0.5))
                frequency = words[term]
                score += (
                    idf
                    * frequency
                    * 2.2
                    / (frequency + 1.2 * (0.25 + 0.75 * length / max(1, average)))
                )
            if score > 0:
                scored.append((key, score))
        return [key for key, _ in sorted(scored, key=lambda x: (-x[1], x[0]))]

    def hybrid(self, graph, task, filters, mode):
        nodes = self.eligible(graph, filters, task.layers)
        if not nodes:
            return []
        sparse = self._lexical(task.query, nodes)[: task.sparse_top_k] if mode != "dense" else []
        dense = []
        if mode != "sparse":
            if self.embedding is None:
                raise ValueError("Derived dense retrieval requires configured embeddings")
            ordered = sorted(nodes)
            cache_key = (
                filters.user_id,
                "derived",
                self.embedding.config.base_url,
                self.embedding.config.model,
            )
            cache = self.store.derived_index_cache
            fingerprints = {
                k: sha256(n.content.encode()).hexdigest()
                for k, n in graph.nodes.items()
                if k[0] in {"EXPERIENCE", "PROFILE"}
            }
            stored = {
                k: value
                for k, value in cache.get(cache_key, {}).items()
                if k in fingerprints and value[0] == fingerprints[k]
            }
            missing = [k for k in ordered if k not in stored]
            if missing:
                embedded = self.embedding.embed_batch([nodes[k].content for k in missing])
                stored.update(
                    {
                        k: (fingerprints[k], vector)
                        for k, vector in zip(missing, embedded, strict=True)
                    }
                )
            vectors = [stored[k][1] for k in ordered]

            # Account conservatively for Python floats and container overhead.
            def size(entries):
                return sum(len(vector) * 40 + 256 for _, vector in entries.values())

            cache.pop(cache_key, None)
            if size(stored) <= self.CACHE_BYTES:
                cache[cache_key] = stored
                while (
                    len(cache) > self.CACHE_ENTRIES
                    or sum(size(v) for v in cache.values()) > self.CACHE_BYTES
                ):
                    cache.popitem(last=False)
            query = self.embedding.embed(task.query)

            def cosine(vector):
                if len(vector) != len(query):
                    raise ValueError("Derived embedding dimensions do not match")
                norm = math.sqrt(sum(v * v for v in vector) * sum(v * v for v in query))
                return sum(a * b for a, b in zip(vector, query, strict=True)) / norm if norm else 0

            dense = [
                k
                for k, _ in sorted(
                    zip(ordered, (cosine(v) for v in vectors), strict=True),
                    key=lambda x: (-x[1], x[0]),
                )[: task.dense_top_k]
            ]
        return [
            RetrievedEvidence(k[0], k[1], 0, derived=graph.record(k))
            for k, _ in reciprocal_rank_fusion([dense, sparse])[: task.top_k]
        ]

    def grep(self, graph, query, filters):
        nodes = self.eligible(graph, filters, query.layers)
        if query.mode == "FTS":
            keys = self._lexical(query.pattern, nodes)
        elif query.mode == "ALL":
            keys = sorted(nodes, key=lambda k: (nodes[k].record.created_at, k))
        elif query.mode == "LITERAL":
            keys = [k for k in sorted(nodes) if query.pattern in nodes[k].content]
        else:
            try:
                compiled = regex.compile(query.pattern, regex.VERSION1)
                deadline, keys = time.monotonic() + 0.2, []
                for key, node in nodes.items():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    if compiled.search(node.content, timeout=remaining):
                        keys.append(key)
            except (regex.error, TimeoutError) as error:
                raise RetrievalError(
                    "Derived grep exceeded its budget or has an invalid pattern"
                ) from error
        return [
            RetrievedEvidence(k[0], k[1], 0, derived=graph.record(k))
            for k in keys[query.offset : query.offset + query.limit]
        ]

    def graph(self, user_id):
        return ProvenanceGraph(self.store, user_id)
