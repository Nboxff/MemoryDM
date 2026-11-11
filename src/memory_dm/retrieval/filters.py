"""Common eligibility predicates, applied before either retrieval branch ranks rows."""

from dataclasses import dataclass
from typing import Literal

from memory_dm.models import date_bounds


@dataclass(frozen=True)
class SearchFilters:
    user_id: str
    session_id: str | None = None
    time_start: str | None = None
    time_end: str | None = None
    time_field: Literal["EVENT_TIME", "RECORDED_TIME"] = "EVENT_TIME"

    def __post_init__(self):
        if not self.user_id.strip():
            raise ValueError("A user ID is required")
        if self.time_field not in ("EVENT_TIME", "RECORDED_TIME"):
            raise ValueError("Time field must be EVENT_TIME or RECORDED_TIME")
        if self.time_start is not None:
            date_bounds(self.time_start)
        if self.time_end is not None:
            date_bounds(self.time_end)
        if self.time_start and self.time_end:
            if date_bounds(self.time_start)[0] > date_bounds(self.time_end)[1]:
                raise ValueError("Search interval ends before it starts")
