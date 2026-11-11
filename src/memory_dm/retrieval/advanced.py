"""Plan every memory query, execute bounded searches and build grounded context."""

import logging
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime

from memory_dm.config import Settings
from memory_dm.errors import RetrievalError
from memory_dm.llm.clients import EmbeddingClient, LLMClient
from memory_dm.llm.structured import ResponseValidationError
from memory_dm.llm.usage import APIUsage
from memory_dm.models import Turn, utc_now
from memory_dm.retrieval.context import BuiltContext, ContextBuilder
from memory_dm.retrieval.executor import RetrievalExecutor
from memory_dm.retrieval.filters import SearchFilters
from memory_dm.retrieval.models import (
    EvidenceAssessment,
    GrepQuery,
    RetrievalLimits,
    RetrievalPlan,
    SearchTask,
)
from memory_dm.retrieval.planner import (
    EvidenceEvaluator,
    EvidenceReviewer,
    QueryPlanner,
    SubjectReviewer,
)
from memory_dm.retrieval.subjects import validate_subjects
from memory_dm.storage.sqlite import ConcurrentUpdateError, SQLiteStore

logger = logging.getLogger(__name__)


@dataclass
class RetrievalTrace:
    latency_ms: float
    rounds: int
    tasks: int
    planner_calls: int
    evaluator_calls: int
    llm: APIUsage
    embedding: APIUsage
    context_token_upper_bound: int
    sufficient: bool | None
    supported_attributes: int = 0
    missing_attributes: int = 0
    conflicted_attributes: int = 0
    query_type: str | None = None
    subject_counts: dict[str, int] = field(default_factory=dict)
    subject_revisions: int = 0
    owner_scope_references: int = 0
    subject_review_calls: int = 0
    subject_review_failures: int = 0
    partial_attributes: int = 0
    evidence_review_calls: int = 0
    evidence_review_failures: int = 0
    recovered_attributes: int = 0


@dataclass
class AdvancedResult:
    plan: RetrievalPlan
    context: BuiltContext
    trace: RetrievalTrace
    data_revision: tuple[int, int]
    assessment: EvidenceAssessment | None = None

    def metrics(self) -> dict:
        return asdict(self.trace)


@dataclass
class _SubjectReviewOutcome:
    assessment: EvidenceAssessment
    reassessed: bool = False
    failed: bool = False


@dataclass
class _RetrievalRequest:
    query: str
    user_id: str
    history: list[Turn]
    reference_time: datetime
    scope: SearchFilters | None
    limits: RetrievalLimits


@dataclass
class _RetrievalRun:
    plan: RetrievalPlan
    assessment_plan: RetrievalPlan
    current_plan: RetrievalPlan
    context: BuiltContext
    trace: RetrievalTrace
    assessment: EvidenceAssessment | None = None
    reviewed_subjects: list | None = None
    evidence: list = field(default_factory=list)
    seen: set = field(default_factory=set)


class AdvancedRetriever:
    def __init__(
        self,
        store: SQLiteStore,
        settings: Settings,
        llm: LLMClient,
        embedding: EmbeddingClient | None = None,
        *,
        mode: str | None = None,
    ):
        self.store, self.settings, self.llm, self.embedding = store, settings, llm, embedding
        layers = ["ATOMIC", "RAW"]
        if settings.experience_retrieval_enabled:
            layers.append("EXPERIENCE")
        if settings.profile_enabled:
            layers.append("PROFILE")
        self.planner = QueryPlanner(
            llm, available_layers=layers, available_methods=settings.retrieval_methods
        )
        self.executor = RetrievalExecutor(
            store,
            embedding,
            mode=mode,
            available_layers=layers,
            available_methods=settings.retrieval_methods,
            neighbor_turns=settings.retrieval_neighbor_turns,
        )
        self.builder = ContextBuilder(
            store,
            token_budget=settings.retrieval_context_tokens,
            char_budget=settings.memory_context_chars,
        )
        self.evaluator = EvidenceEvaluator(
            llm, available_layers=layers, available_methods=settings.retrieval_methods
        )
        self.subject_reviewer = SubjectReviewer(llm)
        self.evidence_reviewer = EvidenceReviewer(llm)

    def _fallback_task(self, query: str, reference_time: datetime | None = None) -> SearchTask:
        if "hybrid" in self.settings.retrieval_methods:
            return SearchTask(query=query, top_k=min(self.settings.memory_top_k, 50))
        if "grep" in self.settings.retrieval_methods:
            return SearchTask(
                query=query,
                layers=["RAW"],
                retrieval_type=["grep"],
                grep_queries=[GrepQuery(pattern=query, mode="FTS")],
            )
        return SearchTask(
            query=query,
            layers=["ATOMIC", "RAW"],
            retrieval_type=["time"],
            time_end=(reference_time or utc_now()).date().isoformat(),
            time_field="RECORDED_TIME",
            top_k=min(self.settings.memory_top_k, 50),
        )

    def _reconsider_identity(
        self,
        query: str,
        user_id: str,
        plan: RetrievalPlan,
        context: BuiltContext,
        reference_time: datetime,
        *,
        history: list[Turn],
        executed_plan: RetrievalPlan,
        assessment: EvidenceAssessment,
    ) -> _SubjectReviewOutcome:
        """Correct a scope-only gap once, then independently recheck the actual facts."""
        try:
            review = self.subject_reviewer.review(
                query, assessment.subjects, context.records, history=history, user_id=user_id
            )
        except RetrievalError:
            logger.warning("Subject review repair failed; retaining the original assessment")
            return _SubjectReviewOutcome(assessment, failed=True)
        if review.subjects == assessment.subjects:
            return _SubjectReviewOutcome(assessment)
        corrected_plan = plan.model_copy(update={"subjects": review.subjects})
        try:
            corrected = self.evaluator.assess(
                query,
                corrected_plan,
                context.records,
                reference_time,
                history=history,
                executed_plan=executed_plan,
                previous_subjects=review.subjects,
                fixed_subjects=review.subjects,
                user_id=user_id,
            )
        except RetrievalError:
            logger.warning("Subject reassessment repair failed; retaining the original assessment")
            return _SubjectReviewOutcome(assessment, reassessed=True, failed=True)
        return _SubjectReviewOutcome(corrected, reassessed=True)

    def retrieve(
        self,
        query: str,
        user_id: str,
        *,
        history: list[Turn] | None = None,
        reference_time: datetime | None = None,
        scope: SearchFilters | None = None,
        plan: RetrievalPlan | None = None,
        limits: RetrievalLimits | None = None,
    ) -> AdvancedResult:
        start = time.perf_counter()
        if scope is not None and scope.user_id != user_id:
            raise ValueError("Retrieval scope belongs to another user")
        request = _RetrievalRequest(
            query,
            user_id,
            history or [],
            reference_time or utc_now(),
            scope,
            limits or RetrievalLimits(),
        )
        before_llm = self.llm.usage()
        before_embedding = self.embedding.usage() if self.embedding else APIUsage()
        revision = self.store.data_revision(user_id)
        planned, failed = self._plan(request, plan)
        trace = RetrievalTrace(
            0, 0, 0, int(plan is None), 0, APIUsage(), APIUsage(), 0, False if failed else None
        )
        run = _RetrievalRun(planned, planned, planned, self.builder.build([], user_id), trace)
        self._search_rounds(request, run)
        self._review_missing(request, run)
        if self.store.data_revision(user_id) != revision:
            raise ConcurrentUpdateError(
                "Memory or raw evidence changed during retrieval planning; retry"
            )
        return self._result(run, revision, start, before_llm, before_embedding)

    def _plan(self, request, plan):
        failed = False
        if plan is None:
            try:
                plan = self.planner.plan(
                    request.query,
                    request.history,
                    request.reference_time,
                    limits=request.limits,
                    user_id=request.user_id,
                )
            except RetrievalError:
                logger.warning("Query plan repair failed; using scoped basic retrieval")
                plan = RetrievalPlan(
                    tasks=[self._fallback_task(request.query, request.reference_time)]
                )
                failed = True
        return request.limits.constrain(plan), failed

    def _search_rounds(self, request, run):
        excluded = (
            {request.history[-1].turn_id}
            if (
                request.history
                and request.history[-1].role == "user"
                and request.history[-1].content == request.query
            )
            else set()
        )
        for _ in range(self.settings.retrieval_max_rounds):
            fresh = [
                task for task in run.current_plan.tasks if task.model_dump_json() not in run.seen
            ]
            if not fresh:
                break
            run.seen.update(task.model_dump_json() for task in fresh)
            run.trace.rounds += 1
            run.trace.tasks += len(fresh)
            run.evidence.extend(
                self.executor.execute(
                    RetrievalPlan(mode=run.current_plan.mode, tasks=fresh),
                    request.user_id,
                    scope=request.scope,
                )
            )
            run.context = self.builder.build(
                run.evidence, request.user_id, excluded_turn_ids=excluded, scope=request.scope
            )
            if not self._assess_round(request, run):
                break
            self._review_identity(request, run)
            run.trace.sufficient = run.assessment.sufficient
            if run.assessment.sufficient or run.assessment.follow_up is None:
                break
            run.current_plan = request.limits.constrain(run.assessment.follow_up)

    def _assess_round(self, request, run):
        run.trace.evaluator_calls += 1
        if run.reviewed_subjects is not None:
            try:
                validate_subjects(
                    run.reviewed_subjects, request.query, request.history[-12:], run.context.records
                )
            except ResponseValidationError:
                run.reviewed_subjects = None
        try:
            run.assessment = self.evaluator.assess(
                request.query,
                run.assessment_plan,
                run.context.records,
                request.reference_time,
                history=request.history,
                executed_plan=run.current_plan,
                previous_subjects=run.assessment.subjects if run.assessment else None,
                fixed_subjects=run.reviewed_subjects,
                user_id=request.user_id,
            )
        except RetrievalError:
            logger.warning("Evidence assessment repair failed; retaining bounded evidence")
            run.trace.sufficient, run.assessment = False, None
            return False
        return True

    def _review_identity(self, request, run):
        if (
            run.trace.subject_review_calls
            or not run.context.records
            or not any(s.target != "CURRENT_USER" for s in run.assessment.subjects)
        ):
            return
        run.trace.subject_review_calls += 1
        outcome = self._reconsider_identity(
            request.query,
            request.user_id,
            run.assessment_plan,
            run.context,
            request.reference_time,
            history=request.history,
            executed_plan=run.current_plan,
            assessment=run.assessment,
        )
        run.trace.evaluator_calls += int(outcome.reassessed)
        run.trace.subject_review_failures += int(outcome.failed)
        run.assessment = outcome.assessment
        if outcome.reassessed and not outcome.failed:
            run.assessment_plan = run.plan.model_copy(update={"subjects": run.assessment.subjects})
            run.reviewed_subjects = run.assessment.subjects

    def _review_missing(self, request, run):
        if (
            run.assessment is None
            or not run.context.records
            or not self.evidence_reviewer.candidates(run.assessment)
        ):
            return
        run.trace.evidence_review_calls = 1
        missing_before = sum(item.status == "MISSING" for item in run.assessment.answer_coverage)
        try:
            run.assessment = self.evidence_reviewer.review(
                request.query,
                run.assessment,
                run.context.records,
                history=request.history,
                user_id=request.user_id,
            )
            run.trace.recovered_attributes = missing_before - sum(
                item.status == "MISSING" for item in run.assessment.answer_coverage
            )
            run.trace.sufficient = run.assessment.sufficient
        except RetrievalError:
            run.trace.evidence_review_failures = 1
            logger.warning("Evidence review repair failed; retaining the original assessment")

    def _result(self, run, revision, start, before_llm, before_embedding):
        trace = run.trace
        trace.latency_ms = round((time.perf_counter() - start) * 1000, 3)
        trace.llm = self.llm.usage().since(before_llm)
        trace.embedding = (
            self.embedding.usage().since(before_embedding) if self.embedding else APIUsage()
        )
        trace.context_token_upper_bound = run.context.token_upper_bound
        if run.assessment is not None:
            counts = Counter(item.status for item in run.assessment.answer_coverage)
            trace.supported_attributes, trace.missing_attributes = (
                counts["SUPPORTED"],
                counts["MISSING"],
            )
            trace.partial_attributes, trace.conflicted_attributes = (
                counts["PARTIAL"],
                counts["CONFLICTED"],
            )
            trace.query_type = run.assessment.query_type
            trace.subject_counts = dict(Counter(s.target for s in run.assessment.subjects))
            initial = {s.id: s for s in run.plan.subjects}
            trace.subject_revisions = sum(
                (s.target, s.basis) != (initial[s.id].target, initial[s.id].basis)
                for s in run.assessment.subjects
                if s.id in initial
            )
            trace.owner_scope_references = sum(
                s.basis == "OWNER_SCOPE" for s in run.assessment.subjects
            )
        return AdvancedResult(run.plan, run.context, trace, revision, run.assessment)
