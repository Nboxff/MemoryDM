"""Execute bounded plans using typed storage reads and existing hybrid retrieval."""

import logging

from memory_dm.errors import RetrievalError
from memory_dm.llm.clients import EmbeddingClient
from memory_dm.retrieval.derived import DerivedRetriever
from memory_dm.retrieval.evidence import RetrievedEvidence
from memory_dm.retrieval.filters import SearchFilters
from memory_dm.retrieval.fusion import EvidenceFusion
from memory_dm.retrieval.models import GrepMatch, GrepQuery, RetrievalPlan, SearchTask
from memory_dm.retrieval.search import HybridRetriever
from memory_dm.storage.atomic import AtomicMemoryStore
from memory_dm.storage.indexing import keyword_span, query_terms
from memory_dm.storage.memory import MemoryStore
from memory_dm.storage.search import SearchRepository
from memory_dm.storage.sqlite import ConcurrentUpdateError, SQLiteStore

logger = logging.getLogger(__name__)


class RetrievalExecutor:
    MAX_CANDIDATES = 150

    def __init__(
        self,
        store: SQLiteStore,
        embedding: EmbeddingClient | None = None,
        *,
        mode: str | None = None,
        available_layers=("ATOMIC", "RAW"),
        available_methods=("hybrid", "grep", "time"),
        neighbor_turns: int = 0,
    ):
        self.store = store
        self.neighbor_turns = neighbor_turns
        self.hybrid = HybridRetriever(store, embedding)
        self.mode = mode or ("hybrid" if embedding else "sparse")
        if self.mode not in ("hybrid", "dense", "sparse"):
            raise ValueError("Search mode must be hybrid, dense or sparse")
        self.memory_store = MemoryStore(store)
        self.atomic = AtomicMemoryStore(store)
        self.repository = SearchRepository(store)
        self.available_layers = frozenset(available_layers)
        self.available_methods = frozenset(available_methods)
        if type(neighbor_turns) is not int or not 0 <= neighbor_turns <= 4:
            raise ValueError("Neighbor turn window must be an integer between 0 and 4")
        self._session_cache: dict[str, list] = {}
        self.derived = DerivedRetriever(store, embedding)

    @staticmethod
    def _scope(query: GrepQuery, task: SearchTask, scope: SearchFilters | None) -> GrepQuery:
        values = query.model_dump()
        if task.session_id is not None:
            values["session_id"] = task.session_id
        if task.time_start is not None or task.time_end is not None:
            values.update(
                time_start=task.time_start, time_end=task.time_end, time_field=task.time_field
            )
        if scope is not None:
            if scope.session_id is not None:
                values["session_id"] = scope.session_id
            if scope.time_start is not None or scope.time_end is not None:
                values.update(
                    time_start=scope.time_start,
                    time_end=scope.time_end,
                    time_field=scope.time_field,
                )
        return GrepQuery.model_validate(values)

    def execute(
        self, plan: RetrievalPlan, user_id: str, *, scope: SearchFilters | None = None
    ) -> list[RetrievedEvidence]:
        if scope is not None and scope.user_id != user_id:
            raise ValueError("Retrieval scope belongs to another user")
        revision = self.store.data_revision(user_id)
        if any(set(task.layers) - self.available_layers for task in plan.tasks):
            raise ValueError("Retrieval plan requests a disabled memory layer")
        if any(set(task.retrieval_type) - self.available_methods for task in plan.tasks):
            raise ValueError("Retrieval plan requests a disabled search method")
        graph = (
            self.derived.graph(user_id)
            if any(set(task.layers) & {"EXPERIENCE", "PROFILE"} for task in plan.tasks)
            else None
        )
        fusion = EvidenceFusion()
        source_cache = {}

        for task in plan.tasks:
            self._execute_task(task, user_id, scope, graph, fusion, source_cache)
        if self.store.data_revision(user_id) != revision:
            raise ConcurrentUpdateError("Memory or raw evidence changed during retrieval; retry")
        # Apply the shared budget after fusion so an early broad branch cannot
        # prevent a later, independently corroborated result from entering context.
        return fusion.ranked(self.MAX_CANDIDATES)

    def _execute_task(self, task, user_id, scope, graph, fusion, source_cache):
        scoped = self._scope(
            GrepQuery(layers=task.layers, mode="ALL", limit=task.top_k), task, scope
        )
        filters = self._filters(user_id, scoped)
        task_memories = {}
        self._search_derived(task, scoped, user_id, scope, graph, fusion, filters)
        if "hybrid" in task.retrieval_type and "ATOMIC" in task.layers:
            self._search_atomic(task, scoped, user_id, fusion, task_memories)
        if "time" in task.retrieval_type:
            fusion.collect(
                self._search_grep(scoped, user_id, task_memories), filters, method="time"
            )
        if "grep" in task.retrieval_type:
            for query in task.grep_queries:
                query = self._scope(query, task, scope)
                fusion.collect(
                    self._search_grep(query, user_id, task_memories),
                    self._filters(user_id, query),
                    method="grep",
                )
        self._expand_graph(task, scoped, user_id, graph, fusion, filters, task_memories)
        self._expand_sources(task, scoped, user_id, fusion, task_memories, source_cache)

    def _search_derived(self, task, scoped, user_id, scope, graph, fusion, filters):
        if graph is not None:
            derived_hits = []
            if "hybrid" in task.retrieval_type:
                derived_hits = self.derived.hybrid(graph, task, filters, self.mode)
                fusion.collect(derived_hits, filters, method="hybrid")
            if "time" in task.retrieval_type:
                temporal = self.derived.grep(graph, scoped, filters)
                fusion.collect(temporal, filters, method="time")
                derived_hits.extend(temporal)
            for query in task.grep_queries:
                grep_query = self._scope(query, task, scope)
                hits = self.derived.grep(graph, grep_query, self._filters(user_id, grep_query))
                fusion.collect(hits, self._filters(user_id, grep_query), method="grep")
                derived_hits.extend(hits)
            if task.need_raw_evidence or task.graph_hops:
                for hit in derived_hits:
                    parents = frozenset({(hit.layer, hit.id)})
                    source_items = []
                    for key in graph.ancestors(
                        (hit.layer, hit.id),
                        max_hops=3 if task.need_raw_evidence else task.graph_hops,
                    ):
                        if key[0] == "ATOMIC":
                            memory = graph.nodes[key].record
                            source_items.append(
                                RetrievedEvidence("ATOMIC", key[1], 0, memory=memory)
                            )
                        elif key[0] == "RAW":
                            turn = graph.nodes[key].record
                            source_items.extend(
                                self._raw_items(
                                    [
                                        GrepMatch(
                                            id=key[1],
                                            layer="RAW",
                                            content=turn.content,
                                            session_id=turn.session_id,
                                            role=turn.role,
                                            timestamp=turn.timestamp.isoformat(),
                                        )
                                    ],
                                    user_id,
                                )
                            )
                        elif key[0] in self.available_layers:
                            source_items.append(
                                RetrievedEvidence(key[0], key[1], 0, derived=graph.record(key))
                            )
                    fusion.expand(source_items, filters, parents=parents, discount=0.65)

    def _search_atomic(self, task, scoped, user_id, fusion, task_memories):
        hits = self.hybrid.search(
            task.query,
            user_id,
            mode=self.mode,
            top_k=task.top_k,
            dense_top_k=task.dense_top_k,
            sparse_top_k=task.sparse_top_k,
            session_id=scoped.session_id,
            time_start=scoped.time_start,
            time_end=scoped.time_end,
            time_field=scoped.time_field,
        )
        task_memories.update((hit.memory.id, hit.memory) for hit in hits)
        fusion.collect(
            [RetrievedEvidence("ATOMIC", hit.memory.id, 0, memory=hit.memory) for hit in hits],
            self._filters(user_id, scoped),
            method="hybrid",
        )

    def _search_grep(self, query, user_id, task_memories):
        layers = [layer for layer in query.layers if layer in {"ATOMIC", "RAW"}]
        if not layers:
            return []
        scoped = query.model_copy(update={"layers": layers})
        try:
            matches = self.memory_store.grep(user_id, scoped)
        except RetrievalError as error:
            # A planner-generated regex can exhaust the scan/time budget on a long
            # conversation. Degrade that branch to lexical search instead of failing
            # the whole question; malformed patterns and other errors still propagate.
            if scoped.mode != "REGEX" or "budget" not in str(error):
                raise
            logger.warning("Grep regex exceeded its budget; retrying as FTS")
            matches = self.memory_store.grep(user_id, scoped.model_copy(update={"mode": "FTS"}))
        items = []
        for match in matches:
            if match.layer == "RAW":
                items.extend(self._raw_items([match], user_id))
            else:
                memory = self.atomic.get(match.id, user_id)
                if memory is not None and memory.status == "ACTIVE":
                    task_memories[memory.id] = memory
                    items.append(RetrievedEvidence("ATOMIC", memory.id, 0, memory=memory))
        return items

    def _expand_graph(self, task, scoped, user_id, graph, fusion, filters, task_memories):
        if task.graph_hops:
            roots = frozenset(("ATOMIC", key) for key in task_memories)
            if graph is not None:
                higher = [
                    RetrievedEvidence(key[0], key[1], 0, derived=graph.record(key))
                    for key in graph.descendants(set(roots), max_hops=task.graph_hops)
                    if key[0] in self.available_layers and key[0] in {"EXPERIENCE", "PROFILE"}
                ]
                fusion.expand(higher, filters, parents=roots, discount=0.65)
            linked = self.memory_store.linked_memories(
                user_id, list(task_memories), hops=task.graph_hops
            )
            # Graph expansion must also respect explicit temporal/session constraints.
            eligible = self.repository.eligible_contents(
                SearchFilters(
                    user_id,
                    scoped.session_id,
                    scoped.time_start,
                    scoped.time_end,
                    scoped.time_field,
                )
            )
            graph_items = []
            for memory in linked:
                if memory.id in eligible:
                    task_memories[memory.id] = memory
                    graph_items.append(RetrievedEvidence("ATOMIC", memory.id, 0, memory=memory))
            fusion.expand(graph_items, filters, parents=roots, discount=0.5)

    def _expand_sources(self, task, scoped, user_id, fusion, task_memories, source_cache):
        if task.need_raw_evidence:
            for memory in task_memories.values():
                # Source expansion is intentionally allowed to show the dates of the
                # supporting original statement, even when filtering the fact's event time.
                if memory.id not in source_cache:
                    sources = self.memory_store.evidence(user_id, memory.id)
                    for match in sources:
                        span = keyword_span(match.content, query_terms(memory.content))
                        if span is not None:
                            match.match_start, match.match_end = span
                    source_cache[memory.id] = self._raw_items(sources, user_id)
                fusion.expand(
                    source_cache[memory.id],
                    SearchFilters(user_id, scoped.session_id),
                    parents=frozenset({("ATOMIC", memory.id)}),
                    discount=0.65,
                )

    @staticmethod
    def _filters(user_id: str, query: GrepQuery) -> SearchFilters:
        return SearchFilters(
            user_id, query.session_id, query.time_start, query.time_end, query.time_field
        )

    def _raw_items(self, matches: list[GrepMatch], user_id: str) -> list[RetrievedEvidence]:
        return [
            RetrievedEvidence("RAW", match.id, 0, raw=self._with_neighbors(match, user_id))
            for match in matches
        ]

    def _with_neighbors(self, match: GrepMatch, user_id: str) -> GrepMatch:
        """Attach the surrounding conversation window to a retrieved source turn.

        ContextBuilder already renders these neighbors compactly; attaching them here
        keeps the window inside the existing token budget instead of adding records.
        """
        if not self.neighbor_turns or not match.session_id:
            return match
        turns = self._session_cache.get(match.session_id)
        if turns is None:
            turns = self.store.get_turns(match.session_id, user_id)
            self._session_cache[match.session_id] = turns
        positions = {turn.turn_id: index for index, turn in enumerate(turns)}
        index = positions.get(match.id)
        if index is None:
            return match
        before = turns[max(0, index - self.neighbor_turns) : index]
        after = turns[index + 1 : index + 1 + self.neighbor_turns]
        if not before and not after:
            return match
        return match.model_copy(
            update={
                "context_before": before or match.context_before,
                "context_after": after or match.context_after,
            }
        )
