"""Temporal scope shared by experience and profile repair."""

from datetime import UTC, date

from memory_dm.models import date_bounds


def derived_in_scope(spec, created_at, valid_time, source_ids, eligible_atoms) -> bool:
    if not (spec.time_start or spec.time_end):
        return True
    lower = date_bounds(spec.time_start)[0] if spec.time_start else date.min
    upper = date_bounds(spec.time_end)[1] if spec.time_end else date.max
    supported_in_period = any(("ATOMIC", key) in eligible_atoms for key in source_ids)
    if spec.time_field == "RECORDED_TIME":
        return lower <= created_at.astimezone(UTC).date() <= upper or supported_in_period
    if valid_time is None:
        return supported_in_period
    start, end = valid_time.point or valid_time.start, valid_time.point or valid_time.end
    return (date_bounds(start)[0] if start else date.min) <= upper and (
        date_bounds(end)[1] if end else date.max
    ) >= lower
