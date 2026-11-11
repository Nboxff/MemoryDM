"""Consolidate changed atomic facts without replaying stale source bodies."""

import json
import sqlite3
from collections import Counter

from memory_dm.errors import RetrievalError
from memory_dm.experience.models import (
    ExperienceMemory,
    ExperienceReport,
    SourceGroupError,
    group_key,
    validate_group,
)
from memory_dm.experience.semantic import ExperienceSemantic
from memory_dm.llm.clients import ClientError, LLMClient
from memory_dm.llm.structured import StructuredResponseError
from memory_dm.models import utc_now
from memory_dm.retrieval.derived import DerivedRetriever
from memory_dm.retrieval.filters import SearchFilters
from memory_dm.retrieval.models import SearchTask
from memory_dm.retrieval.search import HybridRetriever
from memory_dm.storage.experience import ExperienceSnapshot, ExperienceStore
from memory_dm.storage.indexing import query_terms
from memory_dm.storage.sqlite import ConcurrentUpdateError, SQLiteStore


class ExperienceService:
    MAX_FACTS = 40
    MAX_INPUT_CHARS = 24000

    def __init__(self, store: SQLiteStore, llm: LLMClient, embedding=None, *, batch_size=16):
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("Experience batch size must be a positive integer")
        self.store, self.repository = store, ExperienceStore(store)
        self.semantic = ExperienceSemantic(llm)
        self.embedding = embedding
        self.batch_size = batch_size

    def _pending_ids(self, before, session_id):
        if session_id is None:
            return list(before.pending)
        changed = {
            r[0]
            for r in self.store.connection.execute(
                "SELECT memory_id FROM experience_session_changes WHERE session_id=?", (session_id,)
            )
        }
        return [
            key
            for key in before.pending
            if key in changed
            or key not in before.atomic.memories
            or any(e.session_id == session_id for e in before.atomic.memories[key].evidence)
        ]

    def maintain(
        self, user_id: str, *, force: bool = False, session_id: str | None = None
    ) -> ExperienceReport:
        if session_id is not None:
            self.store._require_session(session_id, user_id)
        before = self.repository.snapshot(user_id)
        selected = self._pending_ids(before, session_id)
        report = ExperienceReport(pending_sources=len(selected))
        if not selected:
            return report
        key = self.repository.work_key(before, selected)
        if not force and self.repository.failure_attempts(user_id, key) >= 3:
            report.status = "retry_exhausted"
            return report
        try:
            return self._maintain(user_id, before, report, selected)
        except (
            ClientError,
            StructuredResponseError,
            ValueError,
            sqlite3.Error,
            RetrievalError,
        ) as error:
            # Planned writes are not committed when validation or the CAS check fails.
            report.added = report.updated = report.unchanged = report.processed_sources = 0
            report.memory_ids.clear()
            report.status = "failed"
            report.error_code = (
                "CONCURRENT_UPDATE"
                if isinstance(error, ConcurrentUpdateError)
                else "API_ERROR"
                if isinstance(error, ClientError)
                else "SCHEMA_ERROR"
                if isinstance(error, StructuredResponseError)
                else "STORAGE_ERROR"
                if isinstance(error, sqlite3.Error)
                else "RETRIEVAL_ERROR"
                if isinstance(error, RetrievalError)
                else "SOURCE_VALIDATION_ERROR"
            )
            report.diagnostics = getattr(error, "diagnostics", {}) or {
                "operation": "experience_maintenance",
                "issues": [{"code": report.error_code}],
            }
            try:
                self.repository.record_failure(user_id, key, report.error_code, memory_ids=selected)
            except (ValueError, sqlite3.Error):
                pass
            return report

    def consolidate_session(self, session) -> ExperienceReport:
        """Drain this session's changes, including a final batch below the manual threshold."""
        result = ExperienceReport()
        while True:
            report = self.maintain(session.user_id, session_id=session.session_id)
            result.include(report)
            if (
                report.status in {"failed", "retry_exhausted"}
                or report.processed_sources == 0
                or report.pending_sources == 0
            ):
                return result

    def _context(
        self, user_id: str, before: ExperienceSnapshot, selected=None
    ) -> tuple[dict, list[str], dict]:
        selected = list(before.pending if selected is None else selected)[: self.batch_size]
        active = {key: m for key, m in before.atomic.memories.items() if m.status == "ACTIVE"}
        ordered = [key for key in selected if key in active]
        retriever = HybridRetriever(self.store, self.embedding)
        mode = "hybrid" if self.embedding else "sparse"
        for key in list(ordered):
            ordered.extend(
                hit.memory.id
                for hit in retriever.search(active[key].content, user_id, mode=mode, top_k=12)
            )
        pool, source_ids = {}, set()
        for key in dict.fromkeys(ordered):
            if key not in active or len(pool) >= self.MAX_FACTS:
                continue
            candidate = active[key]
            ids = source_ids | {e.turn_id for e in candidate.evidence}
            if not ids.issubset(before.atomic.turns):
                raise ValueError("An atomic source is missing or belongs to another user")
            size = len(
                json.dumps(
                    [
                        [m.model_dump(mode="json") for m in [*pool.values(), candidate]],
                        [before.atomic.turns[source].model_dump(mode="json") for source in ids],
                    ],
                    ensure_ascii=False,
                )
            )
            if pool and size > self.MAX_INPUT_CHARS:
                continue
            pool[key], source_ids = candidate, ids
        terms = set().union(*(set(query_terms(m.content)) for m in pool.values()))
        related_ids = []
        if pool and before.experiences:
            derived = DerivedRetriever(self.store, self.embedding)
            graph = derived.graph(user_id)
            for key in selected:
                if key not in pool:
                    continue
                related_ids.extend(
                    hit.id
                    for hit in derived.hybrid(
                        graph,
                        SearchTask(query=pool[key].content, layers=["EXPERIENCE"], top_k=8),
                        SearchFilters(user_id),
                        mode,
                    )
                )
        ranks = {key: i for i, key in reversed(list(enumerate(related_ids)))}
        existing = sorted(
            before.experiences.values(),
            key=lambda e: (
                ranks.get(e.id, len(ranks)),
                -sum(key in pool for g in e.evidence_groups for key in g.memory_ids),
                -len(terms.intersection(query_terms(e.content))),
                e.id,
            ),
        )[:8]
        payload = {
            "reference_time": utc_now().isoformat(),
            "changed_memory_ids": [key for key in selected if key in pool],
            "atomic_memories": [m.model_dump(mode="json") for m in pool.values()],
            "source_turns": [
                before.atomic.turns[key].model_dump(mode="json") for key in sorted(source_ids)
            ],
            "existing_experiences": [e.model_dump(mode="json") for e in existing],
        }
        processed = [key for key in selected if key in pool or key not in active]
        return payload, processed, pool

    def _maintain(self, user_id, before, report, selected=None):
        payload, processed, pool = self._context(user_id, before, selected)
        if not pool:
            self.repository.commit(user_id, before, [], processed)
            report.status, report.processed_sources = "completed", len(processed)
            report.pending_sources -= len(processed)
            return report
        proposed = self.semantic.propose(payload, pool, user_id).candidates
        candidates, relearning = [], []
        targets = Counter(c.target_id for c in proposed if c.target_id is not None)
        supplied = {e["id"] for e in payload["existing_experiences"]}
        changed = set(payload["changed_memory_ids"])

        def reject(code, count=1):
            report.rejection_reasons[code] = report.rejection_reasons.get(code, 0) + count

        for candidate in proposed:
            if candidate.target_id is not None and (
                candidate.target_id not in supplied
                or targets[candidate.target_id] > 1
                or before.experiences[candidate.target_id].kind != candidate.kind
            ):
                report.rejected_candidates += 1
                report.rejected_groups += len(candidate.evidence_groups)
                reject("invalid_update_target")
                continue
            eligible = []
            for group in candidate.evidence_groups:
                try:
                    validate_group(group, pool, user_id)
                except SourceGroupError as error:
                    report.rejected_groups += 1
                    reject(error.code)
                else:
                    eligible.append(group)
            if not eligible or not any(changed.intersection(g.memory_ids) for g in eligible):
                report.rejected_candidates += 1
                report.rejected_groups += len(eligible)
                reject("no_changed_source" if eligible else "no_valid_source_group")
                continue
            retained = []
            for group in eligible:
                checks = self.repository.relearning(group, before)
                if any(not check["new_user_turn_ids"] for check in checks):
                    report.revoked_groups += 1
                    continue
                retained.append(group)
                for check in checks:
                    relearning.append(
                        {
                            "candidate_index": len(candidates),
                            "group_index": len(retained) - 1,
                            **check,
                        }
                    )
            if retained:
                candidates.append(candidate.model_copy(update={"evidence_groups": retained}))
        checks = (
            self.semantic.verify(
                {
                    **payload,
                    "candidates": [c.model_dump(mode="json") for c in candidates],
                    "relearning_checks": relearning,
                }
            ).results
            if candidates
            else []
        )
        supported = {(r.candidate_index, r.group_index) for r in checks if r.supported}
        upserts = {}
        for i, candidate in enumerate(candidates):
            groups = [g for j, g in enumerate(candidate.evidence_groups) if (i, j) in supported]
            report.rejected_groups += len(candidate.evidence_groups) - len(groups)
            if not groups:
                continue
            old = upserts.get(candidate.target_id) or before.experiences.get(candidate.target_id)
            if old is not None and old.kind != candidate.kind:
                raise ValueError("An update cannot change the experience kind")
            if old is None:
                old = next(
                    (
                        e
                        for e in [*upserts.values(), *before.experiences.values()]
                        if (e.content, e.kind, e.valid_time)
                        == (candidate.content, candidate.kind, candidate.valid_time)
                    ),
                    None,
                )
            same_claim = old is not None and (old.content, old.kind, old.valid_time) == (
                candidate.content,
                candidate.kind,
                candidate.valid_time,
            )
            if same_claim:
                groups = [*groups, *old.evidence_groups]
            groups = list({group_key(g): g for g in groups}.values())[:16]
            memory = ExperienceMemory(
                user_id=user_id,
                kind=candidate.kind,
                content=candidate.content,
                valid_time=candidate.valid_time,
                evidence_groups=sorted(groups, key=group_key),
                **({"id": old.id, "created_at": old.created_at} if old else {}),
            )
            if memory != old:
                upserts[memory.id] = memory
            if memory.id not in report.memory_ids:
                report.memory_ids.append(memory.id)
        report.added = sum(key not in before.experiences for key in upserts)
        report.updated = len(upserts) - report.added
        report.unchanged = sum(key not in upserts for key in report.memory_ids)
        self.repository.commit(user_id, before, list(upserts.values()), processed)
        report.status, report.processed_sources = "completed", len(processed)
        report.pending_sources -= len(processed)
        return report
