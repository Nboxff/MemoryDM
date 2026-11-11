import calendar
import re
from datetime import UTC, date, datetime
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

MemoryType = Literal["PROFILE", "PREFERENCE", "INTENTION", "INSTRUCTION", "EPISODE"]
Role = Literal["system", "user", "assistant"]
MemoryStatus = Literal["ACTIVE", "INACTIVE", "SUPERSEDED"]


def utc_now() -> datetime:
    return datetime.now(UTC)


def date_bounds(value: str) -> tuple[date, date]:
    # Expand a fuzzy date string into a closed interval [start, end].
    # Example: "2024-02" -> (2024-02-01, 2024-02-29)
    if not re.fullmatch(r"[0-9]{4}(?:-[0-9]{2})?(?:-[0-9]{2})?", value):
        raise ValueError("Dates must use YYYY, YYYY-MM, or YYYY-MM-DD")
    parts = [int(part) for part in value.split("-")]
    year = parts[0]
    month = parts[1] if len(parts) > 1 else 1
    day = parts[2] if len(parts) > 2 else 1
    start = date(year, month, day)
    if len(parts) == 1:
        end = date(year, 12, 31)
    elif len(parts) == 2:
        end = date(year, month, calendar.monthrange(year, month)[1])
    else:
        end = start
    return start, end


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", str_min_length=1)

    @field_validator("*", mode="after")
    @classmethod
    def reject_blank_strings(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("Text must not be blank")
        return value


class Session(Record):
    user_id: str
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    created_at: AwareDatetime = Field(default_factory=utc_now)


class Turn(Record):
    session_id: str
    role: Role
    content: str
    turn_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: AwareDatetime = Field(default_factory=utc_now)


class Evidence(Record):
    session_id: str
    turn_id: str
    span_start: int | None = Field(default=None, ge=0, strict=True)
    span_end: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def check_span(self) -> "Evidence":
        if (self.span_start is None) != (self.span_end is None):
            raise ValueError("Span offsets must both be set or both be null")
        if self.span_start is not None and self.span_end <= self.span_start:
            raise ValueError("span_end must be greater than span_start")
        return self


class ValidTime(Record):
    start: str | None = None
    end: str | None = None
    point: str | None = None

    @field_validator("start", "end", "point")
    @classmethod
    def check_date(cls, value: str | None) -> str | None:
        if value is not None:
            date_bounds(value)
        return value

    @model_validator(mode="after")
    def check_interval(self) -> "ValidTime":
        if self.start and self.end and date_bounds(self.start)[0] > date_bounds(self.end)[1]:
            raise ValueError("The validity interval ends before it starts")
        return self


class AtomicMemory(Record):
    user_id: str
    content: str
    type: MemoryType
    evidence: list[Evidence] = Field(min_length=1)
    id: str = Field(default_factory=lambda: str(uuid4()))
    valid_time: ValidTime | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    status: MemoryStatus = "ACTIVE"
