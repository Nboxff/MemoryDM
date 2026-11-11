"""Incremental extraction, batch reconciliation and atomic provenance-aware writes."""

import sqlite3

from memory_dm.llm.clients import ClientError, EmbeddingClient, LLMClient
from memory_dm.memory.extractor import AtomicMemoryExtractor
from memory_dm.memory.planning import MemoryWritePlanner
from memory_dm.memory.reconciliation import BatchReconciler, ReconciliationError
from memory_dm.memory.state import WriteReport as WriteReport
from memory_dm.memory.state import WriteSnapshot as WriteSnapshot
from memory_dm.memory.validator import MemoryValidator
from memory_dm.models import AtomicMemory, Session, Turn
from memory_dm.retrieval import HybridRetriever
from memory_dm.retrieval.dense import DenseIndex, RetrievalError
from memory_dm.storage.maintenance import MemoryWriteRepository
from memory_dm.storage.sqlite import SQLiteStore


class MemoryWriter:
    def __init__(
        self,
        store: SQLiteStore,
        llm: LLMClient,
        embedding: EmbeddingClient | None = None,
        *,
        output_language: str = "auto",
        context_turns: int = 12,
    ):
        self.store = store
        self.repository = MemoryWriteRepository(store)
        self.validator = MemoryValidator()
        self.planner = MemoryWritePlanner()
        self.embedding = embedding
        self.extractor = AtomicMemoryExtractor(llm, output_language=output_language)
        self.reconciler = BatchReconciler(llm)
        if type(context_turns) is not int or context_turns <= 0:
            raise ValueError("Context turn limit must be a positive integer")
        self.context_turns = context_turns

    def snapshot(self, user_id: str) -> WriteSnapshot:
        return self.repository.snapshot(user_id)

    def pending(self, session: Session) -> list[Turn]:
        return self.repository.pending(session)

    def maintain(self, session: Session) -> WriteReport:
        before = self.snapshot(session.user_id)
        input_hash = self.repository.maintenance_key(before, session)
        try:
            report = self._maintain(session, before)
        except (
            ClientError,
            ValueError,
            sqlite3.Error,
            ReconciliationError,
            RetrievalError,
        ) as error:
            try:
                self.repository.record_failure(session, input_hash, error)
            except (sqlite3.Error, ReconciliationError):
                # A failed bookkeeping write must not hide the original failure.
                pass
            raise
        self.repository.complete_maintenance(session)
        return report

    def _maintain(self, session: Session, before: WriteSnapshot) -> WriteReport:
        history = [turn for turn in before.turns.values() if turn.session_id == session.session_id]
        pending = [turn for turn in history if turn.turn_id not in before.processed]
        if not pending:
            return WriteReport({}, [], "unchanged", 0)
        pending = pending[: self.context_turns]
        first = next(i for i, turn in enumerate(history) if turn.turn_id == pending[0].turn_id)
        last = next(i for i, turn in enumerate(history) if turn.turn_id == pending[-1].turn_id)
        context = history[max(0, first - self.context_turns) : last + 1]
        fresh_ids = [turn.turn_id for turn in pending]
        candidates = (
            self.extractor.extract(session, context, new_turn_ids=fresh_ids)
            if any(turn.role == "user" for turn in pending)
            else []
        )
        return self.integrate(
            candidates, session.user_id, context, pending_turn_ids=fresh_ids, before=before
        )

    def recover_pending(self, user_id: str, *, exclude_session_id: str) -> WriteReport:
        """Attempt one failed old-session chunk; fresh writes retain their own outcome."""
        result = WriteReport({}, [], "unchanged", 0)
        sessions = self.repository.recovery_sessions(user_id, exclude_session_id=exclude_session_id)
        if sessions:
            session = sessions[0]
            try:
                result.include(self.maintain(session))
            except (
                ClientError,
                ValueError,
                sqlite3.Error,
                ReconciliationError,
                RetrievalError,
            ) as error:
                result.recovery_failures.append(
                    {
                        "session_id": session.session_id,
                        "error_type": type(error).__name__,
                        "diagnostics": getattr(error, "diagnostics", {}),
                    }
                )
            else:
                result.recovered_sessions.append(session.session_id)
        result.pending_recovery_sessions = self.repository.pending_recovery_sessions(user_id)
        return result

    def integrate(
        self,
        candidates: list[AtomicMemory],
        user_id: str,
        source_turns: list[Turn],
        *,
        pending_turn_ids: list[str] | None = None,
        before: WriteSnapshot | None = None,
    ) -> WriteReport:
        before = before or self.snapshot(user_id)
        candidates = self._validate_candidates(candidates, user_id, source_turns, before)
        related = self._related_memories(candidates, user_id, before)
        existing_source_ids = {
            e.turn_id for values in related.values() for memory in values for e in memory.evidence
        }
        decisions = self.reconciler.reconcile(
            candidates,
            related,
            source_turns,
            existing_sources={key: before.turns[key] for key in existing_source_ids},
        )
        plan = self.planner.plan(before, candidates, decisions, pending_turn_ids=pending_turn_ids)
        self.repository.commit(user_id, before, plan)
        index_state = self._refresh_dense_index(user_id) if candidates else "sparse_only"
        return WriteReport(plan.counts, plan.memory_ids, index_state, len(plan.processed_turn_ids))

    def _validate_candidates(
        self,
        candidates: list[AtomicMemory],
        user_id: str,
        source_turns: list[Turn],
        before: WriteSnapshot,
    ) -> list[AtomicMemory]:
        for turn in source_turns:
            if before.turns.get(turn.turn_id) != turn:
                raise ReconciliationError("Source conversation changed before reconciliation")
        for memory in candidates:
            if (
                memory.user_id != user_id
                or memory.status != "ACTIVE"
                or memory.id in before.memories
            ):
                raise ReconciliationError(
                    "New memories must have unique IDs and belong to this user"
                )
        candidates = self.validator.validate_batch(candidates, source_turns)
        for memory in candidates:
            if not any(before.turns[e.turn_id].role == "user" for e in memory.evidence):
                raise ReconciliationError("New memories require a supporting user message")
        return candidates

    def _related_memories(
        self, candidates: list[AtomicMemory], user_id: str, before: WriteSnapshot
    ) -> dict[str, list[AtomicMemory]]:
        related = {}
        retriever = HybridRetriever(self.store, self.embedding)
        for memory in candidates:
            hits = retriever.search(
                memory.content,
                user_id,
                mode="hybrid" if self.embedding else "sparse",
                top_k=5,
                dense_top_k=10,
                sparse_top_k=10,
            )
            matches = [hit.memory for hit in hits]
            if any(before.memories.get(match.id) != match for match in matches):
                # Retrieval can observe a commit made while a remote embedding was in flight.
                # Only reconcile against the canonical snapshot whose sources we captured.
                raise ReconciliationError("User data changed during candidate retrieval; retry")
            # Exact matches also cover content with no FTS terms and cannot be crowded out.
            exact = [
                old
                for old in before.memories.values()
                if old.status == "ACTIVE"
                and old.content == memory.content
                and old.type == memory.type
                and old.valid_time == memory.valid_time
            ]
            related[memory.id] = list({m.id: m for m in [*exact, *matches]}.values())[:5]
        return related

    def _refresh_dense_index(self, user_id: str) -> str:
        if self.embedding is None:
            return "sparse_only"
        try:
            DenseIndex(self.store, self.embedding).rebuild(user_id)
            return "ready"
        except (ClientError, RetrievalError, sqlite3.Error):
            # Facts, evidence, FTS and checkpoints have committed. A later query/rebuild
            # recovers the derived index; the canonical write must not be reported as lost.
            return "pending"
