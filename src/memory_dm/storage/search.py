"""SQLite eligibility queries shared by dense, sparse and fused retrieval."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from memory_dm.models import AtomicMemory, date_bounds
from memory_dm.storage.atomic import AtomicMemoryStore
from memory_dm.storage.sqlite import SQLiteStore

if TYPE_CHECKING:
    from memory_dm.retrieval.filters import SearchFilters


def search_predicate(filters: SearchFilters) -> tuple[str, list]:
    """Translate validated filters into parameterized SQL for table alias m."""
    clauses = ["m.user_id = ?", "m.status = 'ACTIVE'"]
    parameters = [filters.user_id]
    if filters.session_id is not None:
        clauses.append("""EXISTS (SELECT 1 FROM memory_evidence AS e
            WHERE e.memory_id = m.memory_id AND e.session_id = ?)""")
        parameters.append(filters.session_id)
    if filters.time_start is not None or filters.time_end is not None:
        lower = (
            date_bounds(filters.time_start)[0].isoformat() if filters.time_start else "0001-01-01"
        )
        upper = date_bounds(filters.time_end)[1].isoformat() if filters.time_end else "9999-12-31"
        if filters.time_field == "RECORDED_TIME":
            clauses.append("julianday(m.created_at) BETWEEN julianday(?) AND julianday(?)")
            parameters.extend([lower + "T00:00:00+00:00", upper + "T23:59:59.999999+00:00"])
        else:
            clauses.extend(
                [
                    "(m.valid_start IS NOT NULL OR m.valid_end IS NOT NULL "
                    "OR m.valid_point IS NOT NULL)",
                    "COALESCE(memory_date_start(m.valid_point), "
                    "memory_date_start(m.valid_start), '0001-01-01') <= ?",
                    "COALESCE(memory_date_end(m.valid_point), "
                    "memory_date_end(m.valid_end), '9999-12-31') >= ?",
                ]
            )
            parameters.extend([upper, lower])
    return " AND ".join(clauses), parameters


def raw_search_predicate(filters: SearchFilters) -> tuple[str, list]:
    """Apply recording or linked event dates to RAW aliases t and s."""
    clauses = ["s.user_id = ?", "t.role IN ('user', 'assistant')"]
    parameters = [filters.user_id]
    if filters.session_id is not None:
        clauses.append("t.session_id = ?")
        parameters.append(filters.session_id)
    if filters.time_start is not None or filters.time_end is not None:
        if filters.time_field == "EVENT_TIME":
            predicate, args = search_predicate(replace(filters, session_id=None))
            clauses.append(
                "EXISTS (SELECT 1 FROM memory_evidence AS e "
                "JOIN atomic_memories AS m ON m.memory_id = e.memory_id "
                "WHERE e.turn_id = t.turn_id AND " + predicate + ")"
            )
        else:
            lower = (
                date_bounds(filters.time_start)[0].isoformat()
                if filters.time_start
                else "0001-01-01"
            )
            upper = (
                date_bounds(filters.time_end)[1].isoformat() if filters.time_end else "9999-12-31"
            )
            clauses.append("julianday(t.timestamp) BETWEEN julianday(?) AND julianday(?)")
            args = [lower + "T00:00:00Z", upper + "T23:59:59.999999Z"]
        parameters.extend(args)
    return " AND ".join(clauses), parameters


class SearchRepository:
    def __init__(self, store: SQLiteStore):
        self.store = store
        self.connection = store.connection
        self.atomic = AtomicMemoryStore(store)

    def _check_session(self, filters: SearchFilters) -> None:
        if (
            filters.session_id is not None
            and self.store.get_session(filters.session_id, filters.user_id) is None
        ):
            raise ValueError("Session not found for this user")

    def eligible_contents(self, filters: SearchFilters) -> dict[str, str]:
        self._check_session(filters)
        where, parameters = search_predicate(filters)
        rows = self.connection.execute(
            f"SELECT m.memory_id, m.content FROM atomic_memories AS m WHERE {where}", parameters
        ).fetchall()
        return {row["memory_id"]: row["content"] for row in rows}

    def eligible_turn_ids(self, filters: SearchFilters, candidates: list[str]) -> set[str]:
        self._check_session(filters)
        predicate, parameters = raw_search_predicate(filters)
        eligible = set()
        for start in range(0, len(candidates), 400):
            chunk = candidates[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                "SELECT t.turn_id FROM conversation_turns AS t "
                "JOIN sessions AS s ON s.session_id = t.session_id "
                f"WHERE {predicate} AND t.turn_id IN ({placeholders})",
                [*parameters, *chunk],
            )
            eligible.update(row["turn_id"] for row in rows)
        return eligible

    def current_memories(
        self,
        filters: SearchFilters,
        candidates: list[str],
        *,
        expected_contents: dict[str, str],
        limit: int,
    ) -> dict[str, AtomicMemory]:
        """Recheck current facts after API calls, hydrating only the final ranked results."""
        self._check_session(filters)
        where, parameters = search_predicate(filters)
        current = {}
        # Bound parameter lists for SQLite builds with a low variable limit.
        for start in range(0, len(candidates), 400):
            chunk = candidates[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.connection.execute(
                f"SELECT m.* FROM atomic_memories AS m WHERE {where} "
                f"AND m.memory_id IN ({placeholders})",
                [*parameters, *chunk],
            ).fetchall()
            current.update({row["memory_id"]: row for row in rows})
        memories = {}
        for key in candidates:
            row = current.get(key)
            if row is None or row["content"] != expected_contents.get(key):
                continue
            memories[key] = self.atomic._from_row(row)
            if len(memories) == limit:
                break
        return memories
