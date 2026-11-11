"""Validated retrieval contracts; user identity is supplied by the application, never a plan."""

from typing import Literal

from pydantic import Field, model_validator

from memory_dm.llm.structured import structured_schema as structured_schema
from memory_dm.models import Record, Turn, date_bounds

MemoryLayer = Literal["ATOMIC", "RAW", "EXPERIENCE", "PROFILE"]
TimeField = Literal["EVENT_TIME", "RECORDED_TIME"]
DATE_PATTERN = r"^[0-9]{4}(?:-[0-9]{2}(?:-[0-9]{2})?)?$"


class GrepQuery(Record):
    layers: list[MemoryLayer] = Field(default_factory=lambda: ["RAW"], min_length=1, max_length=4)
    pattern: str | None = Field(default=None, max_length=256)
    mode: Literal["ALL", "LITERAL", "REGEX", "FTS"] = "LITERAL"
    time_start: str | None = Field(default=None, pattern=DATE_PATTERN)
    time_end: str | None = Field(default=None, pattern=DATE_PATTERN)
    time_field: TimeField = "RECORDED_TIME"
    session_id: str | None = None
    role: Literal["user", "assistant"] | None = None
    before: int = Field(default=0, ge=0, le=4, strict=True)
    after: int = Field(default=0, ge=0, le=4, strict=True)
    limit: int = Field(default=20, ge=1, le=50, strict=True)
    offset: int = Field(default=0, ge=0, le=1000, strict=True)

    @model_validator(mode="after")
    def check_query(self):
        validate_time(self.time_start, self.time_end)
        if len(set(self.layers)) != len(self.layers):
            raise ValueError("Retrieval layers must be unique")
        if self.mode == "ALL":
            if self.pattern is not None:
                raise ValueError("ALL requires a null pattern")
        elif self.pattern is None:
            raise ValueError("Text search requires a pattern")
        if self.role is not None and set(self.layers) != {"RAW"}:
            raise ValueError("Role filters apply to RAW queries only")
        return self


class GrepMatch(Record):
    id: str
    layer: MemoryLayer
    content: str
    session_id: str | None = None
    timestamp: str | None = None
    role: Literal["user", "assistant"] | None = None
    match_start: int | None = None
    match_end: int | None = None
    context_before: list[Turn] = Field(default_factory=list)
    context_after: list[Turn] = Field(default_factory=list)


class SearchTask(Record):
    query: str
    layers: list[MemoryLayer] = Field(
        default_factory=lambda: ["ATOMIC"], min_length=1, max_length=4
    )
    time_start: str | None = Field(default=None, pattern=DATE_PATTERN)
    time_end: str | None = Field(default=None, pattern=DATE_PATTERN)
    time_field: TimeField = "EVENT_TIME"
    session_id: str | None = None
    top_k: int = Field(default=10, ge=1, le=50, strict=True)
    dense_top_k: int = Field(default=10, ge=1, le=50, strict=True)
    sparse_top_k: int = Field(default=10, ge=1, le=50, strict=True)
    graph_hops: int = Field(default=0, ge=0, le=2, strict=True)
    need_raw_evidence: bool = False
    retrieval_type: list[Literal["hybrid", "time", "grep"]] = Field(
        default_factory=lambda: ["hybrid"], min_length=1, max_length=3
    )
    grep_queries: list[GrepQuery] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def check_task(self):
        validate_time(self.time_start, self.time_end)
        if len(set(self.layers)) != len(self.layers) or len(set(self.retrieval_type)) != len(
            self.retrieval_type
        ):
            raise ValueError("Layers and retrieval branches must be unique")
        if "grep" in self.retrieval_type and not self.grep_queries:
            raise ValueError("The grep branch requires at least one structured query")
        if self.grep_queries and "grep" not in self.retrieval_type:
            raise ValueError("Grep queries require the grep branch")
        if any(set(query.layers) - set(self.layers) for query in self.grep_queries):
            raise ValueError("Grep layers must be included in the task layers")
        if "hybrid" in self.retrieval_type and not set(self.layers) - {"RAW"}:
            raise ValueError("Hybrid retrieval requires a structured layer; use grep for RAW")
        if "time" in self.retrieval_type and not (self.time_start or self.time_end):
            raise ValueError("Time retrieval requires an explicit task time boundary")
        return self


class EvidenceReference(Record):
    layer: Literal["ATOMIC", "RAW", "EXPERIENCE", "PROFILE", "WORKING"]
    id: str
    quote: str = Field(max_length=600)


class QuerySubject(Record):
    """A query-local referent, never a stored assertion of the user's name."""

    id: str = Field(pattern=r"^s[1-8]$")
    mention: str | None = Field(default=None, max_length=200)
    query_quote: str | None = Field(default=None, max_length=600)
    evidence: list[EvidenceReference] = Field(default_factory=list, max_length=4)
    basis: Literal[
        "IMPLICIT_OWNER", "FIRST_PERSON", "QUERY_CONTEXT", "OWNER_SCOPE", "MEMORY", "UNRESOLVED"
    ]
    target: Literal["CURRENT_USER", "OTHER_PERSON", "UNRESOLVED"]

    @model_validator(mode="after")
    def check_basis(self):
        if self.basis == "UNRESOLVED":
            if self.target != "UNRESOLVED":
                raise ValueError("An unresolved reference cannot identify a person")
        elif self.target == "UNRESOLVED":
            raise ValueError("An unresolved person requires an unresolved basis")
        if self.basis == "IMPLICIT_OWNER":
            if (
                self.target != "CURRENT_USER"
                or self.mention is not None
                or self.query_quote is not None
            ):
                raise ValueError("An implicit owner has no named mention or query quote")
        if self.basis == "FIRST_PERSON" and self.target != "CURRENT_USER":
            raise ValueError("A first-person reference identifies the current user")
        if self.basis in ("FIRST_PERSON", "QUERY_CONTEXT") and self.query_quote is None:
            raise ValueError("Query-based references require an exact query quote")
        if self.basis == "QUERY_CONTEXT":
            if self.mention is None:
                raise ValueError("An explicit query reference requires a mention")
            if (
                self.target == "CURRENT_USER"
                and self.query_quote.casefold().strip() == self.mention.casefold().strip()
            ):
                raise ValueError("A name alone does not establish a reference to the memory owner")
        if self.basis == "OWNER_SCOPE":
            if self.target != "CURRENT_USER" or self.mention is None:
                raise ValueError("Owner-scoped references require a named current-user referent")
            if self.query_quote is not None:
                raise ValueError("Owner-scoped references use record evidence, not a query quote")
        if self.basis in {"MEMORY", "OWNER_SCOPE"}:
            if not self.evidence:
                raise ValueError("Memory-based references require supporting sources")
        elif self.evidence:
            raise ValueError("Only MEMORY and OWNER_SCOPE references may cite record evidence")
        return self


class RetrievalPlan(Record):
    # Resolve the question before generating searches; this order also appears in
    # the JSON schema supplied to the model.
    query_type: Literal["PERSONAL_MEMORY", "GENERAL", "MIXED"] = "PERSONAL_MEMORY"
    subjects: list[QuerySubject] = Field(default_factory=list, max_length=8)
    answer_targets: list[str] = Field(default_factory=list, max_length=12)
    mode: Literal["DIRECT", "MULTI_STEP"] = "DIRECT"
    tasks: list[SearchTask] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def check_answer_targets(self):
        targets = [item.casefold() for item in self.answer_targets]
        if any(not item.strip() or len(item) > 300 for item in self.answer_targets):
            raise ValueError(
                "Answer targets must be nonblank descriptions of at most 300 characters"
            )
        if len(set(targets)) != len(targets):
            raise ValueError("Answer targets must be unique")
        return self


class SubjectReview(Record):
    """A bounded reconsideration of query referents, without generating new facts."""

    subjects: list[QuerySubject] = Field(min_length=1, max_length=8)


class RetrievalLimits(Record):
    """Caller-controlled ceilings, independent of the model's per-task choices."""

    top_k: int = Field(default=50, ge=1, strict=True)
    dense_top_k: int = Field(default=50, ge=1, strict=True)
    sparse_top_k: int = Field(default=50, ge=1, strict=True)

    def constrain(self, plan: RetrievalPlan) -> RetrievalPlan:
        tasks = [
            task.model_copy(
                update={
                    field: min(getattr(task, field), getattr(self, field))
                    for field in ("top_k", "dense_top_k", "sparse_top_k")
                }
            )
            for task in plan.tasks
        ]
        return plan.model_copy(update={"tasks": tasks})


class AttributeCoverage(Record):
    attribute: str = Field(max_length=300)
    role: Literal["ANSWER", "CONTEXT"]
    subject_ids: list[str] = Field(default_factory=list, max_length=8)
    evidence: list[EvidenceReference] = Field(default_factory=list, max_length=8)
    limitation: str | None = Field(default=None, max_length=300)
    status: Literal["SUPPORTED", "PARTIAL", "MISSING", "CONFLICTED"]
    missing_reason: Literal["IDENTITY", "ATTRIBUTE"] | None = None

    @model_validator(mode="after")
    def check_evidence(self):
        if self.status == "PARTIAL":
            if self.limitation is None:
                raise ValueError("Partial coverage requires a specific source limitation")
        elif self.limitation is not None:
            raise ValueError("Only partial coverage has a source limitation")
        if self.status != "MISSING" and self.missing_reason is not None:
            raise ValueError("Covered attributes cannot have a missing reason")
        if self.status == "MISSING" and self.evidence:
            raise ValueError("Missing attributes cannot cite supporting evidence")
        if self.status != "MISSING" and not self.evidence:
            raise ValueError("Covered attributes require exact evidence references")
        return self


class EvidenceAssessment(Record):
    # Generate bindings and cited evidence before deciding sufficiency. Otherwise an
    # early missing-identity judgment can survive even after its subject is resolved.
    query_type: Literal["PERSONAL_MEMORY", "GENERAL", "MIXED"] = "PERSONAL_MEMORY"
    subjects: list[QuerySubject] = Field(default_factory=list, max_length=8)
    coverage: list[AttributeCoverage] = Field(default_factory=list, max_length=12)
    sufficient: bool
    follow_up: RetrievalPlan | None

    @property
    def answer_coverage(self) -> list[AttributeCoverage]:
        return [item for item in self.coverage if item.role == "ANSWER"]

    @model_validator(mode="after")
    def check_follow_up(self):
        if self.sufficient and self.follow_up is not None:
            raise ValueError("Sufficient evidence cannot request another round")
        if self.query_type == "GENERAL" and (self.follow_up is not None or self.coverage):
            raise ValueError(
                "General knowledge does not require personal evidence or another search"
            )
        attributes = [item.attribute.casefold() for item in self.coverage]
        if len(set(attributes)) != len(attributes):
            raise ValueError("Requested attributes must be unique")
        if self.sufficient and any(
            item.status in {"MISSING", "PARTIAL"} for item in self.answer_coverage
        ):
            raise ValueError("Sufficient evidence cannot leave a requested attribute incomplete")
        return self


class EvidenceReview(Record):
    """Corrections for missing answer attributes, without changing query subjects."""

    coverage: list[AttributeCoverage] = Field(min_length=1, max_length=12)


class AttributeAssessment(Record):
    """Attribute-only model output after query subjects were independently reviewed."""

    query_type: Literal["PERSONAL_MEMORY", "GENERAL", "MIXED"]
    coverage: list[AttributeCoverage] = Field(max_length=12)
    sufficient: bool
    follow_up: RetrievalPlan | None


def validate_time(start: str | None, end: str | None) -> None:
    if start is not None:
        date_bounds(start)
    if end is not None:
        date_bounds(end)
    if start and end and date_bounds(start)[0] > date_bounds(end)[1]:
        raise ValueError("Search interval ends before it starts")
