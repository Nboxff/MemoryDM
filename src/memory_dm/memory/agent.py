"""A memory-enabled conversation loop with semantic routing and scheduled maintenance."""

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import partial
from importlib.resources import files

from memory_dm.config import Settings
from memory_dm.experience.service import ExperienceService
from memory_dm.forgetting import ForgetReport, ForgettingService
from memory_dm.llm.clients import ClientError, EmbeddingClient, LLMClient
from memory_dm.llm.usage import APIUsage
from memory_dm.memory.reconciliation import ReconciliationError
from memory_dm.memory.routing import MemoryRouter
from memory_dm.memory.writer import MemoryWriter, WriteReport
from memory_dm.models import Session, Turn, utc_now
from memory_dm.profile.service import ProfileService
from memory_dm.profile.tokens import token_count
from memory_dm.retrieval.advanced import AdvancedRetriever
from memory_dm.retrieval.dense import RetrievalError
from memory_dm.storage.sqlite import ConcurrentUpdateError, SQLiteStore


@dataclass
class AgentReply:
    content: str
    write: WriteReport | None = None
    forget: ForgetReport | None = None
    maintenance_error: str | None = None
    metrics: dict | None = None
    maintenance_diagnostics: dict | None = None
    routing: dict | None = None


class MemoryAgent:
    def __init__(
        self,
        store: SQLiteStore,
        settings: Settings,
        llm: LLMClient,
        embedding: EmbeddingClient | None = None,
        *,
        before_forget=None,
        progress=None,
    ):
        self.before_forget = before_forget
        self.progress = progress or (lambda label: None)
        self.store = store
        self.settings = settings
        self.llm = llm
        self.embedding = embedding
        self.router = MemoryRouter(
            llm, write_every=settings.memory_write_every, classifier_model=settings.router_model
        )
        self.retriever = AdvancedRetriever(store, settings, llm, embedding)
        self.last_metrics = None
        self._retrieval_metrics = []
        self.writer = MemoryWriter(
            store,
            llm,
            embedding,
            output_language=settings.output_language,
            context_turns=settings.memory_context_turns,
        )

    def maintain(self, session: Session) -> WriteReport:
        """Flush atomic facts; ordinary write thresholds do not consolidate upper layers."""
        self.progress("Remembering...")
        remaining = {turn.turn_id for turn in self.writer.pending(session)}
        result = WriteReport({}, [], "unchanged", 0)
        while remaining:
            report = self.writer.maintain(session)
            if report.processed_turns == 0:
                break
            result.include(report)
            remaining.intersection_update(turn.turn_id for turn in self.writer.pending(session))
        # A failed old session gets at most one chunk, independent of current backlog size.
        result.include(
            self.writer.recover_pending(session.user_id, exclude_session_id=session.session_id)
        )
        return result

    def end_session(self, session: Session) -> WriteReport:
        """Best-effort maintenance: one failed layer cannot block independent layers."""
        result = WriteReport({}, [], "unchanged", 0)

        def failure(stage, error):
            result.maintenance_failures.append(
                {
                    "stage": stage,
                    "error_type": type(error).__name__,
                    "diagnostics": getattr(error, "diagnostics", {})
                    or {"operation": stage, "issues": [{"code": type(error).__name__}]},
                }
            )

        errors = (ClientError, ValueError, sqlite3.Error, ReconciliationError, RetrievalError)
        try:
            result.include(self.maintain(session))
        except errors as error:
            failure("atomic", error)
        self.progress("Updating memory...")
        if self.settings.experience_enabled:
            service = ExperienceService(
                self.store,
                self.llm,
                self.embedding,
                batch_size=self.settings.experience_batch_size,
            )
            try:
                result.experience = service.consolidate_session(session)
            except errors as error:
                failure("experience", error)
        if self.settings.profile_enabled:
            try:
                result.profile = ProfileService(self.store, self.llm).maintain(
                    session.user_id, session_id=session.session_id
                )
            except errors as error:
                failure("profile", error)
        return result

    def _forget(
        self, session: Session, request: str, *, reference_time: datetime | None = None
    ) -> AgentReply:
        self.progress("Forgetting...")
        if self.before_forget is not None:
            self.before_forget()
        report = ForgettingService(
            self.store,
            self.llm,
            self.embedding,
            batch_size=self.settings.forget_batch_size,
            batch_chars=self.settings.forget_batch_chars,
            max_passes=self.settings.forget_max_passes,
            context_turns=self.settings.memory_context_turns,
            probe_count=self.settings.forget_probe_count,
            options=self.settings.forgetting,
            probe_answer=lambda question: (
                MemoryAgent(self.store, self.settings, self.llm, self.embedding)
                .query(session.user_id, question, reference_time=reference_time)
                .content
            ),
        ).forget(
            request, session.user_id, reference_time=reference_time, session_id=session.session_id
        )
        if not report.verified:
            receipt = "Available forgetting changes were saved, but verification is incomplete."
        elif report.replacement_memories:
            receipt = "Forgetting and replacement completed."
        else:
            receipt = "Forgetting completed."
        result = AgentReply(receipt, forget=report)
        if not report.verified:
            # Return the committed partial state before model-driven maintenance can
            # re-extract removed facts from sources that still failed verification.
            return result
        try:
            result.write = self.maintain(session)
        except (
            ClientError,
            ValueError,
            sqlite3.Error,
            ReconciliationError,
            RetrievalError,
        ) as error:
            result.maintenance_error = str(error)
            result.maintenance_diagnostics = getattr(error, "diagnostics", {})
        return result

    def _measure(self, operation, session: Session, content: str) -> AgentReply:
        start = time.perf_counter()
        before_llm = self.llm.usage()
        before_embedding = self.embedding.usage() if self.embedding else APIUsage()
        self._retrieval_metrics = []
        try:
            result = operation(session, content)
        finally:
            self.last_metrics = {
                "latency_ms": round((time.perf_counter() - start) * 1000, 3),
                "llm": asdict(self.llm.usage().since(before_llm)),
                "embedding": asdict(self.embedding.usage().since(before_embedding))
                if self.embedding
                else asdict(APIUsage()),
                "retrieval": self._retrieval_metrics,
            }
        result.metrics = self.last_metrics
        return result

    def forget(
        self, session: Session, request: str, *, reference_time: datetime | None = None
    ) -> AgentReply:
        return self._measure(partial(self._forget, reference_time=reference_time), session, request)

    def respond(self, session: Session, content: str, *, on_text=None) -> AgentReply:
        return self._measure(partial(self._respond, on_text=on_text), session, content)

    def observe(self, session: Session, turn: Turn) -> AgentReply:
        """Ingest an existing utterance through normal routing without generating a reply."""
        if turn.session_id != session.session_id or turn.role not in ("user", "assistant"):
            raise ValueError("Observed turns must belong to this session and a dialogue speaker")
        if turn.role == "assistant":
            self.store.add_turn(turn, session.user_id)
            return AgentReply("")
        history = self.store.get_turns(session.session_id, session.user_id)
        pending_count = sum(turn.role == "user" for turn in self.writer.pending(session))
        self.progress("Thinking...")
        routing = self.router.route(turn.content, history, pending_count + 1)
        if routing.action == "FORGET":
            result = self._forget(session, turn.content, reference_time=turn.timestamp)
            result.routing = asdict(routing)
            return result
        self.store.add_turn(turn, session.user_id)
        result = AgentReply("", routing=asdict(routing))
        pending = self.writer.pending(session)
        if (
            routing.action == "WRITE"
            or sum(turn.role == "user" for turn in pending) >= self.settings.memory_write_every
            or (
                self.settings.memory_write_tokens > 0
                and sum(token_count(turn.content) for turn in pending)
                >= self.settings.memory_write_tokens
            )
        ):
            try:
                result.write = self.maintain(session)
            except (
                ClientError,
                ValueError,
                sqlite3.Error,
                ReconciliationError,
                RetrievalError,
            ) as error:
                result.maintenance_error = str(error)
                result.maintenance_diagnostics = getattr(error, "diagnostics", {})
        return result

    def _messages(self, session: Session, history: list[Turn], retrieved) -> list[Turn]:
        policy = files("memory_dm.prompts").joinpath("respond.md").read_text(encoding="utf-8")
        policy += "\n\n" + files("memory_dm.prompts").joinpath("resolve_subject.md").read_text(
            encoding="utf-8"
        )
        messages = [Turn(session_id=session.session_id, role="system", content=policy)]
        records = retrieved.context.records
        status = {"sufficient": retrieved.trace.sufficient, "rounds": retrieved.trace.rounds}
        assessment = retrieved.assessment
        if assessment is not None:
            status.update(
                query_type=assessment.query_type,
                coverage=[
                    item.model_dump(mode="json", exclude={"evidence": {"__all__": {"quote"}}})
                    for item in assessment.answer_coverage
                ],
                subjects=[
                    subject.model_dump(mode="json", exclude={"evidence": {"__all__": {"quote"}}})
                    for subject in assessment.subjects
                ],
            )
            if assessment.query_type == "GENERAL":
                records = []
            else:
                supported = {
                    (ref.layer, ref.id)
                    for item in assessment.answer_coverage
                    for ref in item.evidence
                }
                # Related facts may help further retrieval, but are not substitute answers
                # when the requested personal relationship has no supporting evidence.
                records = [item for item in records if (item["layer"], item["id"]) in supported]
        elif retrieved.trace.sufficient is False:
            status["coverage_check"] = "unavailable"
        if records or retrieved.trace.sufficient is not None:
            messages.append(
                Turn(
                    session_id=session.session_id,
                    role="user",
                    content=json.dumps(
                        {
                            "retrieved_memories": records,
                            "retrieval_status": status,
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        messages.extend(history[-self.settings.memory_context_turns :])
        return messages

    def query(
        self, user_id: str, question: str, *, reference_time: datetime | None = None
    ) -> AgentReply:
        """Answer in a fresh working context without routing or persisting probe QA."""
        session = Session(user_id=user_id, created_at=reference_time or utc_now())
        return self._measure(self._query, session, question)

    def _query(self, session: Session, content: str) -> AgentReply:
        history = [
            Turn(
                session_id=session.session_id,
                role="user",
                content=content,
                timestamp=session.created_at,
            )
        ]
        for attempt in range(2):
            revision = self.store.data_revision(session.user_id)
            try:
                self.progress("Searching memories...")
                retrieved = self.retriever.retrieve(
                    content,
                    session.user_id,
                    history=history,
                    reference_time=session.created_at,
                )
                self._retrieval_metrics.append(retrieved.metrics())
                self.progress("Writing reply...")
                reply = self.llm.chat(self._messages(session, history, retrieved))
                if self.store.data_revision(session.user_id) != revision:
                    raise ConcurrentUpdateError("Memory changed during the model reply; retry")
            except ConcurrentUpdateError:
                if attempt == 0:
                    continue
                raise
            return AgentReply(reply)

    def _respond(self, session: Session, content: str, *, on_text=None) -> AgentReply:
        result = self.observe(
            session, Turn(session_id=session.session_id, role="user", content=content)
        )
        if result.forget is not None:
            return result
        emitted = False

        def emit(text):
            nonlocal emitted
            emitted = True
            on_text(text)

        for attempt in range(2):
            history = self.store.get_turns(session.session_id, session.user_id)
            revision = self.store.data_revision(session.user_id)
            try:
                self.progress("Searching memories...")
                retrieved = self.retriever.retrieve(
                    content, session.user_id, history=history, reference_time=history[-1].timestamp
                )
            except ConcurrentUpdateError:
                if attempt == 0:
                    continue
                raise
            self._retrieval_metrics.append(retrieved.metrics())
            self.progress("Writing reply...")
            options = {"on_text": emit} if on_text is not None else {}
            reply = self.llm.chat(self._messages(session, history, retrieved), **options)
            try:
                self.store.add_turn(
                    Turn(session_id=session.session_id, role="assistant", content=reply),
                    session.user_id,
                    expected_history=history,
                    expected_data_revision=revision,
                )
            except ConcurrentUpdateError:
                if attempt == 0 and not emitted:
                    continue
                raise
            result.content = reply
            return result
