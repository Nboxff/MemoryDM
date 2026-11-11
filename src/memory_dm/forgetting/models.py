"""Validated inputs, model decisions and content-free forgetting reports."""

from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from memory_dm.models import MemoryType, Record, ValidTime, date_bounds

ForgetScope = Literal["FACT", "TOPIC", "ENTITY", "EPISODE", "TIME_RANGE"]
MemoryLayer = Literal["ATOMIC", "RAW", "EXPERIENCE", "PROFILE"]


class ReplacementTime(ValidTime):
    start: str | None
    end: str | None
    point: str | None


class ReplacementFact(Record):
    """A new claim explicitly supplied in the request, independently verified before storage."""

    content: str
    type: MemoryType
    valid_time: ReplacementTime | None


class ReplacementCorrection(Record):
    index: int = Field(ge=0, strict=True)
    fact: ReplacementFact


class ReplacementCorrections(Record):
    corrections: list[ReplacementCorrection] = Field(min_length=1)


class ForgetSpec(Record):
    original_request: str
    target_description: str
    scope: ForgetScope
    time_start: str | None = None
    time_end: str | None = None
    time_field: Literal["RECORDED_TIME", "EVENT_TIME"] = "RECORDED_TIME"
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    replacements: list[ReplacementFact] = Field(default_factory=list)
    clarification_recovered: bool = Field(default=False, strict=True)
    search_queries: list[str] = Field(default_factory=list, max_length=8)
    match_all: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def check_time(self):
        if self.match_all and self.scope != "TOPIC":
            raise ValueError("Only an explicit all-memory TOPIC can match all records")
        if self.time_start:
            date_bounds(self.time_start)
        if self.time_end:
            date_bounds(self.time_end)
        if self.time_start and self.time_end:
            if date_bounds(self.time_start)[0] > date_bounds(self.time_end)[1]:
                raise ValueError("Forgetting interval ends before it starts")
        if self.scope == "TIME_RANGE" and not (self.time_start or self.time_end):
            raise ValueError("TIME_RANGE requires a time boundary")
        return self


class ParsedRequest(Record):
    target_description: str
    scope: ForgetScope
    time_start: str | None
    time_end: str | None
    time_field: Literal["RECORDED_TIME", "EVENT_TIME"]
    clarification: str | None
    replacements: list[ReplacementFact] = Field(default_factory=list)

    search_queries: list[str] = Field(default_factory=list, max_length=8)
    match_all: bool = Field(default=False, strict=True)


class TargetScopeReview(Record):
    matches_request: bool = Field(strict=True)
    feedback: str | None

    @model_validator(mode="after")
    def check_feedback(self):
        if not self.matches_request and self.feedback is None:
            raise ValueError("A rejected target requires specific scope feedback")
        return self


class ForgetDecision(Record):
    layer: MemoryLayer
    id: str
    operation: Literal["KEEP", "DELETE", "REDACT", "REGENERATE"]
    new_content: str | None
    new_valid_time: ReplacementTime | None

    @model_validator(mode="after")
    def check_replacement(self):
        if self.operation in {"REDACT", "REGENERATE"}:
            if self.new_content is None:
                raise ValueError("REDACT requires replacement content")
        elif self.new_content is not None or self.new_valid_time is not None:
            raise ValueError("KEEP and DELETE cannot have replacement fields")
        if self.layer == "RAW" and self.new_valid_time is not None:
            raise ValueError("Raw turns cannot have event dates")
        if self.operation == "REGENERATE" and self.layer not in {"EXPERIENCE", "PROFILE"}:
            raise ValueError("Regeneration applies only to derived layers")
        if self.layer == "PROFILE" and self.new_valid_time is not None:
            raise ValueError("A profile is Markdown text; preserve dates inside its content")
        return self


class Decisions(Record):
    decisions: list[ForgetDecision]


class VerificationItem(Record):
    layer: MemoryLayer
    id: str
    target_present: bool = Field(strict=True)
    preservation_ok: bool = Field(strict=True)
    supported: bool = Field(strict=True)


VerificationIssueType = Literal["TARGET_REMAINS", "NON_TARGET_LOSS", "UNSUPPORTED_CLAIM"]


class VerificationIssue(Record):
    type: VerificationIssueType
    field: Literal["content", "valid_time.start", "valid_time.end", "valid_time.point"]
    quote: str | None = Field(max_length=600)
    reason: str = Field(max_length=600)
    source_turn_ids: list[str] = Field(default_factory=list, max_length=16)


class VerificationFinding(VerificationItem):
    """Detailed findings live only in the current repair context, not the public report."""

    issues: list[VerificationIssue] = Field(default_factory=list, max_length=6)


class Verification(Record):
    results: list[VerificationFinding]


class ForgetReport(Record):
    """Committed changes and separate record/answer checks; reports never retain forgotten text."""

    request_id: str
    scope: ForgetScope
    deleted_memories: int
    redacted_memories: int
    deleted_turns: int
    redacted_turns: int
    repaired_evidence: int
    verification_passes: int
    verified: bool
    dense_invalidated: bool
    replacement_memories: int = 0
    verification_failures: list[VerificationItem] = Field(default_factory=list)
    clarification_recovered: bool = False
    deleted_experiences: int = 0
    redacted_experiences: int = 0
    experience_verification_passes: int = 0
    # Historical report names; the profile layer now contains at most one document.
    deleted_profile_entries: int = 0
    regenerated_profile_entries: int = 0
    repair_cost: dict[str, int] = Field(default_factory=dict)
    probe_results: list[dict] = Field(default_factory=list)
    verification_scope: str = "retrieved_and_linked"
    committed: bool = True
    storage_verified: bool = True
    semantic_verified: bool | None = None
    probe_verified: bool | None = None
    profile_file_synced: bool | None = None
    operation_failures: list[dict] = Field(default_factory=list)

    @model_validator(mode="after")
    def legacy_verification(self):
        if self.semantic_verified is None:
            self.semantic_verified = self.verified
        return self


class ForgettingError(RuntimeError):
    """A fatal planning or commit error prevents the forgetting write."""


class ForgetResponseError(ForgettingError):
    """A model response failed validation; callers may retain earlier valid batches."""
