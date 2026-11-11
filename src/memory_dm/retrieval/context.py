"""Deduplicated, ranked context with explicit provenance and bounded serialization."""

import json
import math
from dataclasses import dataclass, field

from memory_dm.retrieval.evidence import EvidenceKey, RetrievedEvidence
from memory_dm.retrieval.filters import SearchFilters
from memory_dm.storage.atomic import AtomicMemoryStore
from memory_dm.storage.provenance import ProvenanceGraph
from memory_dm.storage.search import SearchRepository
from memory_dm.storage.sqlite import SQLiteStore


@dataclass
class BuiltContext:
    records: list[dict]
    token_upper_bound: int
    characters: int


@dataclass
class _Candidate:
    record: dict
    score: float
    primary: bool
    parents: set[EvidenceKey] = field(default_factory=set)
    anchor: tuple[int, int] | None = None


class ContextBuilder:
    PRIMARY_SHARE = 0.8
    RAW_MATCH_CHARS = 1000
    RAW_SOURCE_CHARS = 600
    RAW_NEIGHBOR_CHARS = 320
    MIN_EXCERPT_CHARS = 120

    def __init__(self, store: SQLiteStore, *, token_budget: int = 8192, char_budget: int = 16000):
        if min(token_budget, char_budget) <= 0:
            raise ValueError("Context budgets must be positive")
        self.store = store
        self.atomic = AtomicMemoryStore(store)
        self.repository = SearchRepository(store)
        self.token_budget = token_budget
        self.char_budget = char_budget

    @staticmethod
    def _size(records: list[dict]) -> tuple[int, int]:
        if not records:
            return 0, 0
        serialized = json.dumps({"retrieved_memories": records}, ensure_ascii=False)
        # Byte-level tokenizers cannot use more ordinary text tokens than UTF-8 bytes.
        # This deliberately conservative bound is independent of the configured provider.
        return len(serialized.encode("utf-8")), len(serialized)

    def _fits(self, records, *, share=1.0):
        tokens, chars = self._size(records)
        return tokens <= self.token_budget * share and chars <= self.char_budget * share

    @staticmethod
    def _excerpt(candidate: _Candidate, length: int) -> dict:
        record = candidate.record
        content = record["content"]
        start = 0
        if candidate.anchor is not None:
            first, last = candidate.anchor
            start = first - length // 3
            if last - first <= length:
                start = max(start, last - length)
        start = max(0, min(start, len(content) - length))
        return {
            **record,
            "content": content[start : start + length],
            "span_start": start,
            "span_end": start + length,
            "truncated": length < len(content),
        }

    def _preview(self, candidate: _Candidate) -> dict:
        if candidate.record["layer"] != "RAW":
            return candidate.record
        if candidate.primary:
            limit = self.RAW_MATCH_CHARS
        elif "context_of" in candidate.record:
            limit = self.RAW_NEIGHBOR_CHARS
        else:
            limit = self.RAW_SOURCE_CHARS
        return self._excerpt(candidate, min(limit, len(candidate.record["content"])))

    def _pack(self, candidates: dict[EvidenceKey, _Candidate]) -> BuiltContext:
        """Prefer useful compact evidence, reserving space for sources and neighbors."""
        previews = {key: self._preview(candidate) for key, candidate in candidates.items()}
        ranked = sorted(
            candidates,
            key=lambda key: (
                -candidates[key].score / math.sqrt(max(1, self._size([previews[key]])[0])),
                *key,
            ),
        )
        primary = [key for key in ranked if candidates[key].primary]
        auxiliary = [key for key in ranked if not candidates[key].primary]
        records, support_records, selected = [], [], set()

        def include(key, share, *, supporting=False):
            candidate, record = candidates[key], previews[key]
            if key in selected:
                return
            if supporting and not candidate.parents.intersection(selected):
                return

            def fits(trial):
                return self._fits([*records, trial], share=share) and (
                    not supporting
                    or self._fits([*support_records, trial], share=1 - self.PRIMARY_SHARE)
                )

            if not fits(record) and record["layer"] == "RAW":
                best = None
                low = min(self.MIN_EXCERPT_CHARS, len(record["content"]))
                high = len(record["content"]) - 1
                while low <= high:
                    length = (low + high) // 2
                    trial = self._excerpt(candidate, length)
                    if fits(trial):
                        best, low = trial, length + 1
                    else:
                        high = length - 1
                record = best
            if record is not None and fits(record):
                records.append(record)
                selected.add(key)
                if supporting:
                    support_records.append(record)

        for key in primary:
            include(key, self.PRIMARY_SHARE if auxiliary else 1.0)
        # Graph facts can themselves own sources. Two passes cover this bounded chain.
        for _ in range(2):
            for key in auxiliary:
                include(key, 1.0, supporting=True)
        for key in primary:
            include(key, 1.0)
        tokens, chars = self._size(records)
        return BuiltContext(records, tokens, chars)

    def build(
        self,
        evidence: list[RetrievedEvidence],
        user_id: str,
        *,
        excluded_turn_ids: set[str] | None = None,
        scope: SearchFilters | None = None,
    ) -> BuiltContext:
        records = _ContextRecords(self, evidence, user_id, excluded_turn_ids, scope)
        for item in evidence:
            records.add_evidence(item)
        return self._pack(records.candidates)


class _ContextRecords:
    """Rehydrate current, user-scoped evidence during one context build."""

    def __init__(self, builder, evidence, user_id, excluded_turn_ids, scope):
        self.store, self.atomic, self.repository = (
            builder.store,
            builder.atomic,
            builder.repository,
        )
        self.user_id, self.scope = (user_id, scope)
        self.excluded_turn_ids = excluded_turn_ids or set()
        if scope is not None and scope.user_id != user_id:
            raise ValueError("Context scope belongs to another user")
        self.candidates, self.atomic_eligibility, self.raw_eligibility = ({}, {}, {})
        self.raw_ids = {
            key
            for item in evidence
            if item.raw is not None
            for key in [
                item.raw.id,
                *[t.turn_id for t in item.raw.context_before + item.raw.context_after],
            ]
        }
        self.current_turns = self.store.get_turns_by_ids(list(self.raw_ids), user_id)
        self.graph = (
            ProvenanceGraph(self.store, user_id) if any(item.derived for item in evidence) else None
        )

    def add_evidence(self, item):
        if item.derived is not None:
            self._add_derived(item)
        elif item.memory is not None:
            self._add_atomic(item)
        elif item.raw is not None:
            self._add_raw(item)

    def eligible(self, record_id, filters, *, raw=False):
        if filters is None:
            return True
        if filters.user_id != self.user_id:
            return False
        cache = self.raw_eligibility if raw else self.atomic_eligibility
        if filters not in cache:
            cache[filters] = (
                self.repository.eligible_turn_ids(filters, list(self.raw_ids))
                if raw
                else self.repository.eligible_contents(filters)
            )
        return record_id in cache[filters]

    def add(self, record, score, *, primary=True, parents=frozenset(), anchor=None):
        key = (record["layer"], record["id"])
        previous = self.candidates.get(key)
        if previous is None:
            self.candidates[key] = _Candidate(record, score, primary, set(parents), anchor)
            return
        previous.parents.update(parents)
        previous.score = max(previous.score, score)
        if (
            primary
            and (not previous.primary)
            or (primary == previous.primary and anchor is not None and (previous.anchor is None))
        ):
            previous.record, previous.anchor = (record, anchor)
        previous.primary |= primary

    def raw_record(
        self,
        turn_id,
        content,
        session_id,
        timestamp,
        role,
        score,
        *,
        primary,
        parents=frozenset(),
        anchor=None,
        relation=None,
    ):
        record = {
            "id": turn_id,
            "layer": "RAW",
            "content": content,
            "session_id": session_id,
            "timestamp": timestamp,
            "role": role,
            "historical": True,
            "span_start": 0,
            "span_end": len(content),
            "truncated": False,
        }
        if relation is not None:
            record["context_of"], record["relative_position"] = relation
        self.add(record, score, primary=primary, parents=parents, anchor=anchor)

    def _add_derived(self, item):
        key = (item.layer, item.id)
        node = self.graph.nodes.get(key)
        if node is None or self.graph.record(key) != item.derived:
            return
        if any(
            filters is not None
            and (
                not self.graph.supported(key, set(self.repository.eligible_contents(filters)))
                or (
                    key[0] == "PROFILE"
                    and (filters.session_id or filters.time_start or filters.time_end)
                )
            )
            for filters in (self.scope, item.scope)
        ):
            return
        self.add(item.derived, item.score, primary=item.primary, parents=item.parents)

    def _add_atomic(self, item):
        memory = item.memory
        if (
            memory.user_id != self.user_id
            or memory.status != "ACTIVE"
            or self.atomic.get(memory.id, self.user_id) != memory
        ):
            return
        partners = self.atomic.active_conflicts(memory.id, self.user_id)
        for candidate in [memory, *partners]:
            if not self.eligible(candidate.id, self.scope) or not self.eligible(
                candidate.id, item.scope
            ):
                continue
            self.add(
                {
                    "id": candidate.id,
                    "layer": "ATOMIC",
                    "content": candidate.content,
                    "type": candidate.type,
                    "valid_time": candidate.valid_time.model_dump()
                    if candidate.valid_time
                    else None,
                    "conflicted": bool(partners),
                },
                item.score,
                primary=item.primary and candidate.id == memory.id,
                parents=item.parents
                if candidate.id == memory.id
                else frozenset({("ATOMIC", memory.id)}),
            )

    def _add_raw(self, item):
        match = item.raw
        if match.id in self.excluded_turn_ids:
            return
        current = self.current_turns.get(match.id)
        if (
            current is None
            or current.content != match.content
            or current.session_id != match.session_id
            or (not self.eligible(match.id, self.scope, raw=True))
            or (not self.eligible(match.id, item.scope, raw=True))
        ):
            return
        self.raw_record(
            current.turn_id,
            current.content,
            current.session_id,
            current.timestamp.isoformat(),
            current.role,
            item.score,
            primary=item.primary,
            parents=item.parents,
            anchor=(match.match_start, match.match_end)
            if match.match_start is not None and match.match_end is not None
            else None,
        )
        neighbors = [
            (turn, index - len(match.context_before))
            for index, turn in enumerate(match.context_before)
        ] + [(turn, index + 1) for index, turn in enumerate(match.context_after)]
        for turn, position in neighbors:
            if (
                turn.turn_id in self.excluded_turn_ids
                or self.current_turns.get(turn.turn_id) != turn
                or (not self.eligible(turn.turn_id, self.scope, raw=True))
                or (not self.eligible(turn.turn_id, item.scope, raw=True))
            ):
                continue
            self.raw_record(
                turn.turn_id,
                turn.content,
                turn.session_id,
                turn.timestamp.isoformat(),
                turn.role,
                item.score * 0.5,
                primary=False,
                parents=frozenset({("RAW", match.id)}),
                relation=(match.id, position),
            )
