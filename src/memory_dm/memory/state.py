"""Plain values exchanged by memory planning, persistence and conversation services."""

from dataclasses import dataclass, field
from typing import Literal

from memory_dm.experience.models import ExperienceReport
from memory_dm.models import AtomicMemory, Turn


@dataclass
class WriteSnapshot:
    memories: dict[str, AtomicMemory]
    turns: dict[str, Turn]
    processed: set[str]
    revision: int


@dataclass(frozen=True)
class MemoryLink:
    source_id: str
    target_id: str
    operation: Literal["MERGE", "CORRECT", "SUPERSEDE", "KEEP_BOTH"]


@dataclass
class WritePlan:
    """Only changed facts, their relationships and the processing checkpoint to commit."""

    upserts: list[AtomicMemory]
    links: list[MemoryLink]
    processed_turn_ids: list[str]
    memory_ids: list[str]
    counts: dict[str, int]


@dataclass
class WriteReport:
    counts: dict[str, int]
    memory_ids: list[str]
    index_state: str
    processed_turns: int
    recovered_sessions: list[str] = field(default_factory=list)
    recovery_failures: list[dict] = field(default_factory=list)
    pending_recovery_sessions: list[str] = field(default_factory=list)
    experience: ExperienceReport | None = None
    profile: dict | None = None
    maintenance_failures: list[dict] = field(default_factory=list)

    def include(self, other: "WriteReport") -> None:
        """Combine committed chunks without hiding a pending derived-index refresh."""
        for operation, count in other.counts.items():
            self.counts[operation] = self.counts.get(operation, 0) + count
        self.memory_ids = list(dict.fromkeys([*self.memory_ids, *other.memory_ids]))
        self.processed_turns += other.processed_turns
        priority = {"unchanged": 0, "sparse_only": 1, "ready": 2, "pending": 3}
        if priority[other.index_state] > priority[self.index_state]:
            self.index_state = other.index_state
        self.recovered_sessions.extend(other.recovered_sessions)
        self.recovery_failures.extend(other.recovery_failures)
        self.pending_recovery_sessions = other.pending_recovery_sessions
        self.maintenance_failures.extend(other.maintenance_failures)
        if other.experience is not None:
            self.experience = other.experience
        if other.profile is not None:
            self.profile = other.profile


def derived_failures(report: WriteReport | None) -> list[dict]:
    """Expose maintenance failures without source text or model responses."""
    if report is None:
        return []
    failures = list(report.maintenance_failures)
    if report.experience and report.experience.status in {"failed", "retry_exhausted"}:
        failures.append(
            {
                "stage": "experience",
                "error_type": report.experience.error_code or report.experience.status,
                "diagnostics": report.experience.diagnostics,
            }
        )
    if report.profile and report.profile["status"] in {"failed", "retry_exhausted"}:
        failures.append(
            {
                "stage": "profile",
                "error_type": report.profile.get("error_code") or report.profile["status"],
                "diagnostics": report.profile.get("diagnostics", {}),
            }
        )
    return failures
