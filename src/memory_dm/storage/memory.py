"""Structured, user-scoped memory reads; no shell commands or generated SQL are accepted."""

import time
from datetime import datetime

import regex

from memory_dm.errors import RetrievalError
from memory_dm.models import AtomicMemory, Turn
from memory_dm.retrieval.filters import SearchFilters
from memory_dm.retrieval.models import GrepMatch, GrepQuery
from memory_dm.storage.atomic import AtomicMemoryStore
from memory_dm.storage.fts import FTSIndexStore
from memory_dm.storage.indexing import keyword_span, match_query, query_terms
from memory_dm.storage.raw_fts import RawFTSStore
from memory_dm.storage.search import raw_search_predicate, search_predicate
from memory_dm.storage.sqlite import SQLiteStore


class MemoryStore:
    def __init__(self, store: SQLiteStore, *, max_scan: int = 10000, regex_timeout: float = 0.2):
        self.store = store
        self.connection = store.connection
        self.atomic = AtomicMemoryStore(store)
        self.max_scan = max_scan
        self.regex_timeout = regex_timeout

    def grep(self, user_id: str, query: GrepQuery) -> list[GrepMatch]:
        if not user_id.strip():
            raise ValueError("A user ID is required")
        query = GrepQuery.model_validate(query.model_dump())
        if (
            query.session_id is not None
            and self.store.get_session(query.session_id, user_id) is None
        ):
            raise ValueError("Session not found for this user")
        if self.connection.in_transaction:
            raise RetrievalError("Grep requires a connection without an open transaction")
        compiled = None
        if query.mode == "REGEX":
            try:
                compiled = regex.compile(query.pattern, regex.VERSION1)
            except regex.error as error:
                raise RetrievalError("Invalid grep regular expression") from error
        if "RAW" in query.layers:
            RawFTSStore(self.store).synchronize()
        if "ATOMIC" in query.layers:
            FTSIndexStore(self.store).synchronize()
        matches = []
        ranks = {}
        if set(query.layers) & {"EXPERIENCE", "PROFILE"}:
            # Import locally: storage's public query contract can search every layer,
            # while feature policy is enforced by the application/executor.
            from memory_dm.retrieval.derived import DerivedRetriever

            derived = DerivedRetriever(self.store)
            graph = derived.graph(user_id)
            filters = SearchFilters(
                user_id, query.session_id, query.time_start, query.time_end, query.time_field
            )
            hits = derived.grep(
                graph,
                query.model_copy(update={"offset": 0, "limit": query.offset + query.limit}),
                filters,
            )
            for rank, hit in enumerate(hits):
                record = graph.nodes[(hit.layer, hit.id)].record
                matches.append(
                    GrepMatch(
                        id=hit.id,
                        layer=hit.layer,
                        content=hit.derived["content"],
                        timestamp=record.created_at.isoformat(),
                    )
                )
                ranks[(hit.layer, hit.id)] = rank
        terms = query_terms(query.pattern) if query.mode == "FTS" else []
        deadline = time.monotonic() + self.regex_timeout
        scanned = 0
        # Matching and neighboring turns share a short read snapshot. There are no API calls.
        with self.connection:
            self.connection.execute("BEGIN")
            for layer in query.layers:
                if layer not in {"ATOMIC", "RAW"}:
                    continue
                sql, parameters = self._query(user_id, query, layer)
                needed = query.offset + query.limit
                rows = self.connection.execute(sql, parameters)
                count = 0
                for row in rows:
                    start = end = None
                    if compiled is not None:
                        scanned += 1
                        if scanned > self.max_scan or time.monotonic() >= deadline:
                            raise RetrievalError("Regex search exceeded its scan or time budget")
                        try:
                            result = compiled.search(
                                row["content"], timeout=max(0.001, deadline - time.monotonic())
                            )
                        except TimeoutError as error:
                            raise RetrievalError("Regex search exceeded its time budget") from error
                        if result is None:
                            continue
                        start, end = result.span()
                    elif query.mode == "LITERAL":
                        start = row["content"].find(query.pattern)
                        end = start + len(query.pattern)
                    elif query.mode == "FTS":
                        span = keyword_span(row["content"], terms)
                        if span is not None:
                            start, end = span
                    matches.append(self._match(row, layer, query, start, end))
                    ranks[(layer, row["id"])] = count
                    count += 1
                    if count >= needed:
                        break
        if query.mode == "FTS":
            # BM25 scales differ between indexes. Merge their ranks, never their scores.
            matches.sort(key=lambda item: (ranks[(item.layer, item.id)], item.layer, item.id))
        else:
            matches.sort(
                key=lambda item: (
                    datetime.fromisoformat(item.timestamp).timestamp(),
                    item.layer,
                    item.id,
                )
            )
        return matches[query.offset : query.offset + query.limit]

    def _query(self, user_id: str, query: GrepQuery, layer: str) -> tuple[str, list]:
        if layer == "ATOMIC":
            where, parameters = search_predicate(
                SearchFilters(
                    user_id, query.session_id, query.time_start, query.time_end, query.time_field
                )
            )
            sql = (
                "SELECT m.memory_id AS id, m.content, m.created_at AS timestamp "
                "FROM atomic_memories AS m"
            )
            fts_table, id_name, alias = "atomic_memories_fts", "memory_id", "m"
        else:
            sql = """SELECT t.rowid AS position, t.turn_id AS id, t.content, t.timestamp,
                        t.session_id, t.role FROM conversation_turns AS t
                     JOIN sessions AS s ON s.session_id = t.session_id"""
            where, parameters = raw_search_predicate(
                SearchFilters(
                    user_id, query.session_id, query.time_start, query.time_end, query.time_field
                )
            )
            if query.role is not None:
                where += " AND t.role = ?"
                parameters.append(query.role)
            fts_table, id_name, alias = "conversation_turns_fts", "turn_id", "t"
        if query.mode == "LITERAL":
            where += f" AND instr({alias}.content, ?) > 0"
            parameters.append(query.pattern)
        elif query.mode == "FTS":
            expression = match_query(query.pattern, natural_language=True)
            if not expression:
                where += " AND 0"
            else:
                sql += f" JOIN {fts_table} ON {fts_table}.{id_name} = {alias}.{id_name}"
                where += f" AND {fts_table} MATCH ?"
                parameters.append(expression)
        if layer == "RAW":
            date_expression = f"{alias}.timestamp"
        elif query.time_field == "EVENT_TIME":
            # Timeline order: sort facts by when the event happened, not when it was saved.
            date_expression = (
                f"COALESCE({alias}.valid_point, {alias}.valid_start, {alias}.created_at)"
            )
        else:
            date_expression = f"{alias}.created_at"
        order = f"julianday({date_expression}), {alias}.{id_name}"
        if query.mode == "FTS" and expression:
            order = f"bm25({fts_table}), julianday({date_expression}) DESC, {alias}.{id_name}"
        return sql + " WHERE " + where + " ORDER BY " + order, parameters

    def _match(self, row, layer, query, start=None, end=None) -> GrepMatch:
        match = GrepMatch(
            id=row["id"],
            layer=layer,
            content=row["content"],
            timestamp=row["timestamp"],
            match_start=start,
            match_end=end,
        )
        if layer == "RAW":
            match.session_id, match.role = row["session_id"], row["role"]
            match.context_before = self._neighbors(
                row["session_id"], row["position"], query.before, preceding=True
            )
            match.context_after = self._neighbors(
                row["session_id"], row["position"], query.after, preceding=False
            )
        return match

    def _neighbors(self, session_id, position, limit, *, preceding):
        if not limit:
            return []
        operator, direction = ("<", "DESC") if preceding else (">", "ASC")
        rows = self.connection.execute(
            f"""SELECT * FROM conversation_turns WHERE session_id = ? AND rowid {operator} ?
                AND role IN ('user', 'assistant') ORDER BY rowid {direction} LIMIT ?""",
            (session_id, position, limit),
        ).fetchall()
        turns = [Turn.model_validate(dict(row)) for row in rows]
        return list(reversed(turns)) if preceding else turns

    def evidence(self, user_id: str, memory_id: str, *, limit: int = 20) -> list[GrepMatch]:
        rows = self.connection.execute(
            """SELECT t.rowid AS position, t.turn_id AS id, t.content, t.timestamp,
                      t.session_id, t.role FROM conversation_turns AS t
               JOIN sessions AS s ON s.session_id = t.session_id
               JOIN memory_evidence AS e ON e.turn_id = t.turn_id
               JOIN atomic_memories AS m ON m.memory_id = e.memory_id
               WHERE m.memory_id = ? AND m.user_id = ? AND s.user_id = ?
                 AND m.status = 'ACTIVE' AND t.role IN ('user', 'assistant')
               GROUP BY t.turn_id ORDER BY t.rowid LIMIT ?""",
            (memory_id, user_id, user_id, min(limit, 50)),
        ).fetchall()
        return [self._match(row, "RAW", GrepQuery(mode="ALL", before=1, after=1)) for row in rows]

    def linked_memories(self, user_id: str, ids: list[str], *, hops: int = 1) -> list[AtomicMemory]:
        visited, frontier, found = set(ids), set(ids), []
        for _ in range(min(hops, 2)):
            next_frontier = set()
            for key in sorted(frontier):
                rows = self.connection.execute(
                    """SELECT DISTINCT m.* FROM memory_links AS l
                       JOIN atomic_memories AS owner ON owner.memory_id = ?
                       JOIN atomic_memories AS m ON m.memory_id = CASE
                           WHEN l.source_id = owner.memory_id THEN l.target_id ELSE l.source_id END
                       WHERE (l.source_id = owner.memory_id OR l.target_id = owner.memory_id)
                         AND owner.user_id = ? AND owner.status = 'ACTIVE'
                         AND m.user_id = ? AND m.status = 'ACTIVE' ORDER BY m.memory_id LIMIT 50""",
                    (key, user_id, user_id),
                ).fetchall()
                for row in rows:
                    if row["memory_id"] not in visited:
                        memory = self.atomic._from_row(row)
                        found.append(memory)
                        visited.add(memory.id)
                        next_frontier.add(memory.id)
                        if len(found) >= 50:
                            return found
            frontier = next_frontier
        return found
