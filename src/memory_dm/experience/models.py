"""Experience claims and AND/OR provenance, independent of atomic fact types."""

from dataclasses import dataclass, field
from datetime import date
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field, model_validator

from memory_dm.models import AtomicMemory, Record, ValidTime, date_bounds, utc_now

ExperienceKind = Literal["HABIT", "PREFERENCE", "WORKFLOW", "SKILL", "TOOL_STRATEGY"]


class SourceGroup(Record):
    """Members jointly support the claim; separate groups support it independently."""

    basis: Literal["EXPLICIT", "OBSERVED"]
    memory_ids: list[str] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_sources(self):
        if len(set(self.memory_ids)) != len(self.memory_ids):
            raise ValueError("A source group cannot repeat a memory ID")
        self.memory_ids = sorted(self.memory_ids)
        return self


class ExperienceCandidate(Record):
    target_id: str | None
    kind: ExperienceKind
    content: str = Field(max_length=1200)
    valid_time: ValidTime | None
    evidence_groups: list[SourceGroup] = Field(min_length=1, max_length=4)


class ExperienceCandidates(Record):
    candidates: list[ExperienceCandidate] = Field(max_length=8)


class ExperienceMemory(Record):
    user_id: str
    kind: ExperienceKind
    content: str
    evidence_groups: list[SourceGroup] = Field(min_length=1, max_length=16)
    valid_time: ValidTime | None = None
    id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def canonical_groups(self):
        if len({group_key(g) for g in self.evidence_groups}) != len(self.evidence_groups):
            raise ValueError("Independent source groups cannot be duplicates")
        self.evidence_groups = sorted(self.evidence_groups, key=group_key)
        return self


class GroupCheck(Record):
    candidate_index: int = Field(ge=0, strict=True)
    group_index: int = Field(ge=0, strict=True)
    supported: bool = Field(strict=True)
    reason: str = Field(max_length=500)


class ExperienceVerification(Record):
    results: list[GroupCheck]


@dataclass
class ExperienceReport:
    status: str = "unchanged"
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    rejected_groups: int = 0
    revoked_groups: int = 0
    processed_sources: int = 0
    pending_sources: int = 0
    memory_ids: list[str] = field(default_factory=list)
    error_code: str | None = None
    diagnostics: dict = field(default_factory=dict)
    rejected_candidates: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)

    def include(self, other: "ExperienceReport") -> None:
        for name in (
            "added",
            "updated",
            "unchanged",
            "rejected_groups",
            "revoked_groups",
            "processed_sources",
            "rejected_candidates",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.memory_ids = list(dict.fromkeys(self.memory_ids + other.memory_ids))
        self.pending_sources = other.pending_sources
        self.status, self.error_code = other.status, other.error_code
        self.diagnostics = other.diagnostics
        for code, count in other.rejection_reasons.items():
            self.rejection_reasons[code] = self.rejection_reasons.get(code, 0) + count


def group_key(group: SourceGroup) -> tuple:
    return group.basis, tuple(sorted(group.memory_ids))


class SourceGroupError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_group(group: SourceGroup, memories: dict[str, AtomicMemory], user_id: str) -> None:
    for memory_id in group.memory_ids:
        memory = memories.get(memory_id)
        if memory is None or memory.user_id != user_id or memory.status != "ACTIVE":
            raise SourceGroupError(
                "unavailable_source",
                "Experience sources must be available ACTIVE facts of this user",
            )
    if group.basis == "EXPLICIT":
        return
    # Recording the same event repeatedly does not create independent observations.
    # Count only nonoverlapping event intervals, never message/extraction timestamps.
    intervals = []
    for memory_id in group.memory_ids:
        memory = memories[memory_id]
        time = memory.valid_time
        if memory.type != "EPISODE" or time is None:
            continue
        start, end = time.point or time.start, time.point or time.end
        if start and end:
            intervals.append((date_bounds(start)[0], date_bounds(end)[1]))
    last, count = date.min, 0
    for start, end in sorted(set(intervals), key=lambda item: (item[1], item[0])):
        if start > last:
            last, count = end, count + 1
    if count < 3:
        raise SourceGroupError(
            "insufficient_observations",
            "Observed patterns require at least three independent dated events",
        )
