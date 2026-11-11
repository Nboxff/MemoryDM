"""Turn applicable verification findings into grounded, transient repair feedback."""

from memory_dm.forgetting.models import (
    VerificationFinding,
    VerificationIssue,
    VerificationIssueType,
)


def _field_value(record: dict, field: str, *, original: bool) -> str | None:
    prefix = "original_" if original else ""
    if field == "content":
        return record[prefix + "content"]
    time = record.get(prefix + "valid_time") or {}
    return time.get(field.removeprefix("valid_time."))


def _grounded_issue(record: dict, issue: VerificationIssue) -> dict | None:
    value = _field_value(record, issue.field, original=issue.type == "NON_TARGET_LOSS")
    # A quoted erased value in original_content is not evidence of a surviving target.
    if issue.quote is not None and (value is None or issue.quote not in value):
        return None
    sources = [*record.get("remaining_sources", []), *record.get("context", [])]
    allowed = {source["turn_id"] for source in sources}
    if record["layer"] == "RAW":
        allowed.add(record["id"])
    return issue.model_copy(
        update={"source_turn_ids": [key for key in issue.source_turn_ids if key in allowed]}
    ).model_dump(mode="json")


def _fallback_issue(record: dict, issue_type: VerificationIssueType) -> dict:
    # Missing locations must not abort otherwise usable best-effort changes. Supply the
    # actual field for reinspection without inventing an offending subspan or new fact.
    reasons = {
        "TARGET_REMAINS": (
            "No grounded residual-target location was provided. Reinspect the current result."
        ),
        "NON_TARGET_LOSS": (
            "No grounded non-target loss location was provided. "
            "Compare original and current fields."
        ),
        "UNSUPPORTED_CLAIM": (
            "No grounded unsupported-claim location was provided. Inspect remaining_sources."
        ),
    }
    fields = ("content", "valid_time.start", "valid_time.end", "valid_time.point")
    field = "content"
    for candidate in fields:
        value = _field_value(record, candidate, original=issue_type == "NON_TARGET_LOSS")
        if value is not None:
            field = candidate
            break
    return {
        "type": issue_type,
        "field": field,
        "quote": None,
        "reason": reasons[issue_type],
        "source_turn_ids": [],
    }


def repair_feedback(record: dict, finding: VerificationFinding) -> dict | None:
    changed = (
        record["content"] != record["original_content"]
        or record["valid_time"] != record["original_valid_time"]
    )
    orphaned_deletion = (
        record["layer"] in {"ATOMIC", "EXPERIENCE"}
        and record["content"] is None
        and not record["remaining_sources"]
    )
    preservation_failed = changed and not finding.preservation_ok and not orphaned_deletion
    unsupported = (
        record["layer"] in {"ATOMIC", "EXPERIENCE"}
        and record["content"] is not None
        and record["support_check_required"]
        and not finding.supported
    )
    types: list[VerificationIssueType] = []
    if finding.target_present:
        types.append("TARGET_REMAINS")
    if preservation_failed:
        types.append("NON_TARGET_LOSS")
    if unsupported:
        types.append("UNSUPPORTED_CLAIM")
    if not types:
        return None
    issues = []
    for issue_type in types:
        grounded = []
        for issue in finding.issues:
            if issue.type == issue_type:
                detail = _grounded_issue(record, issue)
                if detail is not None:
                    grounded.append(detail)
        issues.extend(grounded or [_fallback_issue(record, issue_type)])
    return {
        "target_present": finding.target_present,
        "preservation_ok": not preservation_failed,
        "supported": not unsupported,
        "issues": issues,
    }
