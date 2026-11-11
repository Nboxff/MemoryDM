"""LLM-generated retrieval plans and bounded evidence assessment."""

import json
from datetime import datetime
from importlib.resources import files

from memory_dm.errors import RetrievalError
from memory_dm.llm.clients import LLMClient
from memory_dm.llm.structured import (
    ResponseValidationError,
    StructuredResponseError,
    request_structured,
)
from memory_dm.models import Record, Turn
from memory_dm.retrieval.models import (
    AttributeAssessment,
    AttributeCoverage,
    EvidenceAssessment,
    EvidenceReview,
    QuerySubject,
    RetrievalLimits,
    RetrievalPlan,
    SubjectReview,
)
from memory_dm.retrieval.subjects import owner_scope, validate_subject_coverage, validate_subjects


def _validate_references(coverage: list[AttributeCoverage], sources: dict) -> None:
    for attribute in coverage:
        for reference in attribute.evidence:
            content = sources.get((reference.layer, reference.id))
            if content is None or reference.quote not in content:
                raise ResponseValidationError(
                    "Coverage must cite an available (layer, id) and an exact displayed quote"
                )


def structured_request(
    client: LLMClient,
    model: type[Record],
    prompt_name: str,
    payload: dict,
    *,
    validate=None,
    temperature: float | None = None,
):
    prompt = files("memory_dm.prompts").joinpath(prompt_name + ".md").read_text(encoding="utf-8")
    prompt += "\n\n" + files("memory_dm.prompts").joinpath("resolve_subject.md").read_text(
        encoding="utf-8"
    )
    messages = [
        Turn(session_id=prompt_name, role="system", content=prompt),
        Turn(session_id=prompt_name, role="user", content=json.dumps(payload, ensure_ascii=False)),
    ]
    try:
        return request_structured(
            client, model, messages, name=prompt_name, validate=validate, temperature=temperature
        )
    except StructuredResponseError as error:
        raise RetrievalError(f"Invalid structured response for {prompt_name}") from error


class QueryPlanner:
    def __init__(
        self,
        client: LLMClient,
        *,
        available_layers=("ATOMIC", "RAW"),
        available_methods=("hybrid", "grep", "time"),
    ):
        self.client = client
        self.available_layers = tuple(available_layers)
        self.available_methods = tuple(available_methods)

    def _validate_strategy(self, plan: RetrievalPlan) -> None:
        if not plan.answer_targets:
            raise ResponseValidationError(
                "List the question's requested answer_targets before planning searches"
            )
        branches = {branch for task in plan.tasks for branch in task.retrieval_type}
        if branches - set(self.available_methods):
            raise ResponseValidationError("Search only the supplied available_methods")
        # Pure period listings and exact source lookups are deliberate exceptions.
        if branches in ({"time"}, {"grep"}):
            return
        required = {"hybrid", "grep"} & set(self.available_methods)
        if not required.issubset(branches):
            description = "hybrid and agentic grep" if len(required) == 2 else next(iter(required))
            raise ResponseValidationError(
                f"Normal memory queries require {description}. Use only available_methods. "
                "Time-only period listings and grep-only exact lookups are exceptions."
            )
        if "grep" in self.available_methods and not any(
            "RAW" in query.layers for task in plan.tasks for query in task.grep_queries
        ):
            raise ResponseValidationError(
                "Normal memory plans must include a RAW grep query to check original sources, "
                "including information not yet extracted into ATOMIC."
            )

    def plan(
        self,
        query: str,
        history: list[Turn],
        reference_time: datetime,
        *,
        limits: RetrievalLimits | None = None,
        user_id: str | None = None,
    ) -> RetrievalPlan:
        if not query.strip():
            raise ValueError("Search query must not be blank")

        def validate(plan: RetrievalPlan) -> None:
            if any(set(task.layers) - set(self.available_layers) for task in plan.tasks):
                raise ResponseValidationError("Search only the supplied available_layers")
            self._validate_strategy(plan)
            if plan.query_type != "GENERAL" and not plan.subjects:
                raise ResponseValidationError(
                    "Resolve the personal query subjects before retrieval"
                )
            if plan.query_type == "GENERAL" and plan.subjects:
                raise ResponseValidationError("General knowledge has no personal query subjects")
            validate_subjects(plan.subjects, query, history[-12:])

        return structured_request(
            self.client,
            RetrievalPlan,
            "query_plan",
            {
                "query": query,
                "reference_time": reference_time.isoformat(),
                "conversation_context": [turn.model_dump(mode="json") for turn in history[-12:]],
                "available_layers": list(self.available_layers),
                "available_methods": list(self.available_methods),
                "candidate_limits": (limits or RetrievalLimits()).model_dump(),
                "owner_scope": owner_scope(user_id),
            },
            validate=validate,
            temperature=0,
        )


class SubjectReviewer:
    """Revisit identity-only gaps independently from search planning and fact coverage."""

    def __init__(self, client: LLMClient):
        self.client = client

    def review(
        self,
        query: str,
        subjects: list[QuerySubject],
        context: list[dict],
        *,
        history: list[Turn],
        user_id: str,
    ) -> SubjectReview:
        working = history[-12:]

        def validate(result: SubjectReview) -> None:
            validate_subjects(result.subjects, query, working, context)
            validate_subject_coverage(subjects, result.subjects, [], query_turn_ids=set())

        return structured_request(
            self.client,
            SubjectReview,
            "review_subjects",
            {
                "query": query,
                # Review the references from scratch; showing the earlier classifier's
                # labels and selected citations anchors the review to the same mistake.
                "references": [
                    subject.model_dump(include={"id", "mention"}) for subject in subjects
                ],
                "retrieved_evidence": context,
                "conversation_context": [turn.model_dump(mode="json") for turn in working],
                "owner_scope": owner_scope(user_id),
            },
            validate=validate,
            temperature=0,
        )


class EvidenceEvaluator:
    def __init__(
        self,
        client: LLMClient,
        *,
        available_layers=("ATOMIC", "RAW"),
        available_methods=("hybrid", "grep", "time"),
    ):
        self.client = client
        self.available_layers = tuple(available_layers)
        self.available_methods = tuple(available_methods)

    def assess(
        self,
        query: str,
        plan: RetrievalPlan,
        context: list[dict],
        reference_time: datetime,
        *,
        history: list[Turn] | None = None,
        executed_plan: RetrievalPlan | None = None,
        previous_subjects: list[QuerySubject] | None = None,
        fixed_subjects: list[QuerySubject] | None = None,
        user_id: str | None = None,
    ) -> EvidenceAssessment:
        working = (history or [])[-12:]
        sources = {(item["layer"], item["id"]): item["content"] for item in context}
        sources.update({("WORKING", turn.turn_id): turn.content for turn in working})

        def validate(result: EvidenceAssessment) -> None:
            if result.follow_up and any(
                set(task.retrieval_type) - set(self.available_methods)
                for task in result.follow_up.tasks
            ):
                raise ResponseValidationError("Follow-up tasks must use available_methods only")
            if result.follow_up and any(
                set(task.layers) - set(self.available_layers) for task in result.follow_up.tasks
            ):
                raise ResponseValidationError("Follow-up tasks must use available_layers only")
            if result.query_type != "GENERAL":
                validate_subjects(result.subjects, query, working, context)
                validate_subject_coverage(
                    plan.subjects or result.subjects,
                    result.subjects,
                    result.coverage,
                    query_turn_ids={
                        turn.turn_id
                        for turn in working
                        if turn.role == "user" and turn.content == query
                    },
                )
            elif result.subjects:
                raise ResponseValidationError(
                    "A general question needs no personal subject bindings"
                )
            if result.query_type != "GENERAL" and not result.answer_coverage:
                raise ResponseValidationError(
                    "List the requested personal attributes and their coverage"
                )
            if result.query_type != "GENERAL" and plan.answer_targets:
                expected = {item.casefold() for item in plan.answer_targets}
                actual = {item.attribute.casefold() for item in result.answer_coverage}
                if expected != actual:
                    raise ResponseValidationError(
                        "ANSWER coverage must match the original answer_targets exactly. "
                        f"Expected: {plan.answer_targets}. Related facts belong to CONTEXT; "
                        "do not replace a requested relationship with its constituent facts."
                    )
                if result.follow_up and result.follow_up.answer_targets:
                    if {item.casefold() for item in result.follow_up.answer_targets} != expected:
                        raise ResponseValidationError(
                            "Follow-up searches cannot change answer_targets"
                        )
            _validate_references(result.coverage, sources)

        payload = {
            "query": query,
            "available_layers": list(self.available_layers),
            "available_methods": list(self.available_methods),
            "plan": plan.model_dump(mode="json"),
            "executed_plan": (executed_plan or plan).model_dump(mode="json"),
            "previous_subjects": [
                subject.model_dump(mode="json") for subject in previous_subjects or []
            ],
            "reference_time": reference_time.isoformat(),
            "retrieved_evidence": context,
            "conversation_context": [turn.model_dump(mode="json") for turn in working],
            "owner_scope": owner_scope(user_id),
        }
        if fixed_subjects is not None:
            validate_subjects(fixed_subjects, query, working, context)
            payload["fixed_subjects"] = [
                subject.model_dump(mode="json") for subject in fixed_subjects
            ]

            def bind(result: AttributeAssessment) -> EvidenceAssessment:
                return EvidenceAssessment(**result.model_dump(), subjects=fixed_subjects)

            # Identity review and attribute evaluation have separate responsibilities.
            # Do not let a second classifier undo a reviewed binding over the same sources.
            result = structured_request(
                self.client,
                AttributeAssessment,
                "evaluate_evidence",
                payload,
                validate=lambda result: validate(bind(result)),
                temperature=0,
            )
            return bind(result)
        return structured_request(
            self.client,
            EvidenceAssessment,
            "evaluate_evidence",
            payload,
            validate=validate,
            temperature=0,
        )


class EvidenceReviewer:
    """Recheck missing attributes once without changing subjects or passed coverage."""

    def __init__(self, client: LLMClient):
        self.client = client

    @staticmethod
    def candidates(assessment: EvidenceAssessment) -> list[AttributeCoverage]:
        return [
            item
            for item in assessment.answer_coverage
            if item.status == "MISSING" and item.missing_reason != "IDENTITY"
        ]

    def review(
        self,
        query: str,
        assessment: EvidenceAssessment,
        context: list[dict],
        *,
        history: list[Turn],
        user_id: str,
    ) -> EvidenceAssessment:
        missing = {item.attribute: item for item in self.candidates(assessment)}
        if not missing:
            return assessment
        working = history[-12:]
        sources = {(item["layer"], item["id"]): item["content"] for item in context}
        sources.update(
            {("WORKING", turn.turn_id): turn.content for turn in working if turn.content != query}
        )

        def validate(result: EvidenceReview) -> None:
            attributes = [item.attribute for item in result.coverage]
            if len(attributes) != len(missing) or set(attributes) != set(missing):
                raise ResponseValidationError(
                    "Review exactly the supplied missing attributes once each; "
                    "do not add, drop or rewrite an already covered attribute"
                )
            for item in result.coverage:
                if item.role != "ANSWER" or set(item.subject_ids) != set(
                    missing[item.attribute].subject_ids
                ):
                    raise ResponseValidationError(
                        "Preserve each missing attribute's ANSWER role and subject IDs"
                    )
            validate_subject_coverage(
                assessment.subjects,
                assessment.subjects,
                result.coverage,
                query_turn_ids={turn.turn_id for turn in working if turn.content == query},
            )
            _validate_references(result.coverage, sources)

        result = structured_request(
            self.client,
            EvidenceReview,
            "review_evidence",
            {
                "query": query,
                "query_type": assessment.query_type,
                "subjects": [subject.model_dump(mode="json") for subject in assessment.subjects],
                "missing_answers": [item.model_dump(mode="json") for item in missing.values()],
                "retrieved_evidence": context,
                "conversation_context": [turn.model_dump(mode="json") for turn in working],
                "owner_scope": owner_scope(user_id),
            },
            validate=validate,
            temperature=0,
        )
        corrections = {item.attribute: item for item in result.coverage}
        coverage = [corrections.get(item.attribute, item) for item in assessment.coverage]
        return EvidenceAssessment(
            query_type=assessment.query_type,
            subjects=assessment.subjects,
            coverage=coverage,
            sufficient=all(
                item.status == "SUPPORTED" for item in coverage if item.role == "ANSWER"
            ),
            follow_up=None,
        )
