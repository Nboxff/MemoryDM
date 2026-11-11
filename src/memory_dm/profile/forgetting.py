"""Directly edit and verify the single profile; there are no source dependencies."""

from dataclasses import dataclass

from memory_dm.forgetting.feedback import repair_feedback
from memory_dm.forgetting.models import VerificationItem
from memory_dm.profile.models import UserProfile


@dataclass
class ProfileForgetPlan:
    entries: dict
    failures: list
    passes: int = 0


def plan_profile_forgetting(
    spec,
    before,
    semantic,
    batches,
    max_passes,
    *,
    runner,
):
    current = dict(before.profiles)
    if not current:
        return ProfileForgetPlan({}, [])
    feedback, failures, passes = {}, [], 0

    def records():
        return [
            {
                "layer": "PROFILE",
                "id": key,
                "eligible": True,
                "content": current[key].content if key in current else None,
                "original_content": original.content,
                "valid_time": None,
                "original_valid_time": None,
                "support_check_required": False,
                "remaining_sources": [],
                "profile_has_sources": False,
                **({"verification_feedback": feedback[key]} if key in feedback else {}),
            }
            for key, original in before.profiles.items()
        ]

    for _ in range(max_passes):
        passes += 1
        previous = dict(current)
        for batch in batches(records()):
            result = runner.call("decision", batch, semantic.decide, spec, batch)
            for decision in result.decisions if result is not None else []:
                if decision.operation == "DELETE":
                    current.pop(decision.id, None)
                elif decision.operation in {"REDACT", "REGENERATE"}:
                    old = before.profiles[decision.id]
                    current[decision.id] = UserProfile.model_validate(
                        old.model_copy(update={"content": decision.new_content}).model_dump()
                    )
        feedback, failures = {}, []
        for batch in batches(records()):
            by_id = {r["id"]: r for r in batch}
            result = runner.call("verification", batch, semantic.verify, spec, batch)
            for finding in result.results if result is not None else []:
                failed = repair_feedback(by_id[finding.id], finding)
                if failed:
                    feedback[finding.id] = failed
                    failures.append(VerificationItem(**finding.model_dump(exclude={"issues"})))
                else:
                    runner.verified(finding.layer, finding.id)
        rejected = {key for key, failure in feedback.items() if not failure["preservation_ok"]}
        rejected.update(
            key
            for stage, layer, key in runner.failures
            if stage == "verification" and layer == "PROFILE"
        )
        for key in rejected:
            if key in previous:
                current[key] = previous[key]
            else:
                current.pop(key, None)
        if not feedback and not any(layer == "PROFILE" for layer, _ in runner.retry_keys):
            break
    return ProfileForgetPlan(current, failures, passes)
