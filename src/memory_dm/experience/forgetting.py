"""Repair explicit experience targets after base sources have been repaired."""

from dataclasses import dataclass

from memory_dm.experience.models import (
    ExperienceCandidate,
    ExperienceMemory,
    SourceGroup,
    group_key,
    validate_group,
)
from memory_dm.experience.semantic import ExperienceSemantic
from memory_dm.forgetting.feedback import repair_feedback
from memory_dm.forgetting.models import ForgetSpec, ForgettingError, VerificationItem
from memory_dm.forgetting.scope import derived_in_scope
from memory_dm.models import AtomicMemory, Turn
from memory_dm.storage.experience import roots, source_fingerprint


@dataclass
class ExperienceForgetPlan:
    experiences: dict[str, ExperienceMemory]
    failures: list[VerificationItem]
    passes: int


def _eligible(spec: ForgetSpec, experience: ExperienceMemory, eligible_atoms: set) -> bool:
    ids = {key for group in experience.evidence_groups for key in group.memory_ids}
    return derived_in_scope(spec, experience.created_at, experience.valid_time, ids, eligible_atoms)


def _supported_groups(
    experience: ExperienceMemory,
    groups: list[SourceGroup],
    memories: dict[str, AtomicMemory],
    turns: dict[str, Turn],
    verifier: ExperienceSemantic,
) -> list[SourceGroup]:
    candidates = [
        ExperienceCandidate(
            target_id=None,
            kind=experience.kind,
            content=experience.content,
            valid_time=experience.valid_time,
            evidence_groups=groups[start : start + 4],
        )
        for start in range(0, len(groups), 4)
    ]
    ids = {key for group in groups for key in group.memory_ids}
    source_ids = set().union(*(roots(group, memories) for group in groups))
    checked = verifier.verify(
        {
            "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
            "atomic_memories": [memories[key].model_dump(mode="json") for key in sorted(ids)],
            "source_turns": [turns[key].model_dump(mode="json") for key in sorted(source_ids)],
            "existing_experiences": [],
            "relearning_checks": [],
        }
    )
    return [
        candidates[r.candidate_index].evidence_groups[r.group_index]
        for r in checked.results
        if r.supported
    ]


def plan_experience_forgetting(
    spec,
    before,
    memories,
    turns,
    semantic,
    batches,
    eligible_atoms,
    max_passes,
    *,
    source_revalidation=True,
    runner,
    previous_plan=None,
    previous_ids=(),
):
    repair = _ExperienceRepair(
        spec,
        before,
        memories,
        turns,
        semantic,
        batches,
        eligible_atoms,
        source_revalidation,
        runner,
        previous_plan,
        previous_ids,
    )
    return repair.run(max_passes)


class _ExperienceRepair:
    """Stage source revalidation, decisions and verification independently."""

    def __init__(
        self,
        spec,
        before,
        memories,
        turns,
        semantic,
        batches,
        eligible_atoms,
        source_revalidation,
        runner,
        previous_plan,
        previous_ids,
    ):
        self.previous_plan, self.previous_ids = previous_plan, set(previous_ids)
        self.spec, self.before, self.memories, self.turns = (spec, before, memories, turns)
        self.semantic, self.batches, self.eligible_atoms = (semantic, batches, eligible_atoms)
        self.source_revalidation, self.runner = (source_revalidation, runner)
        self.verifier = ExperienceSemantic(semantic.client)
        self.supporting, self.unsupported, self.current, self.feedback = ({}, set(), {}, {})
        self.passes = 0

    def run(self, max_passes):
        self._revalidate_sources()
        if not self.current:
            return ExperienceForgetPlan({}, [], 0)
        for _ in range(max_passes):
            self.passes += 1
            previous = dict(self.current)
            self._decide()
            self._verify()
            rejected = {
                key for (_, key), failure in self.feedback.items() if not failure["preservation_ok"]
            }
            rejected.update(
                key
                for stage, layer, key in self.runner.failures
                if stage == "verification" and layer == "EXPERIENCE"
            )
            for key in rejected:
                if key in previous:
                    self.current[key] = previous[key]
                else:
                    self.current.pop(key, None)
            if not self.feedback and (
                not any((layer == "EXPERIENCE" for layer, _ in self.runner.retry_keys))
            ):
                break
        return self._finish()

    def _revalidate_sources(self):
        for key, baseline in self.before.experiences.items():
            original = (
                self.previous_plan.experiences.get(key, baseline)
                if self.previous_plan is not None
                else baseline
            )
            unchanged, changed = ([], [])
            for group in original.evidence_groups:
                retained = group.model_copy(
                    update={
                        "memory_ids": [
                            source
                            for source in group.memory_ids
                            if source in self.memories and self.memories[source].status == "ACTIVE"
                        ]
                    }
                )
                try:
                    if not retained.memory_ids:
                        continue
                    validate_group(retained, self.memories, original.user_id)
                except ValueError:
                    continue
                intact = retained == group and all(
                    source_fingerprint(self.before.memories[source], self.before.turns)
                    == source_fingerprint(self.memories[source], self.turns)
                    for source in group.memory_ids
                )
                (unchanged if intact else changed).append(retained)
            valid = (
                (
                    self.runner.call(
                        "source_verification",
                        [{"layer": "EXPERIENCE", "id": key}],
                        _supported_groups,
                        original,
                        changed,
                        self.memories,
                        self.turns,
                        self.verifier,
                    )
                    or []
                    if self.source_revalidation
                    else changed
                )
                if changed
                else []
            )
            groups = list({group_key(g): g for g in [*unchanged, *valid]}.values())
            if not groups and changed:
                groups = list({group_key(g): g for g in changed}.values())
                self.unsupported.add(key)
            if groups:
                self.supporting[key] = original.model_copy(update={"evidence_groups": groups})
        self.current = dict(self.supporting)
        if self.previous_plan is not None:
            for key in self.previous_ids - self.previous_plan.experiences.keys():
                self.current.pop(key, None)

    def _records(self):
        values = []
        for key, original in self.before.experiences.items():
            item, supported = (self.current.get(key), self.supporting.get(key))
            evidence = item or supported
            if item is None and key in self.unsupported:
                evidence = None
            groups = evidence.evidence_groups if evidence else []
            ids = set().union(*(roots(group, self.memories) for group in groups))
            values.append(
                {
                    "layer": "EXPERIENCE",
                    "id": key,
                    "eligible": _eligible(self.spec, original, self.eligible_atoms),
                    "content": item.content if item else None,
                    "original_content": original.content,
                    "valid_time": item.valid_time.model_dump()
                    if item and item.valid_time
                    else None,
                    "original_valid_time": original.valid_time.model_dump()
                    if original.valid_time
                    else None,
                    "source_groups": [group.model_dump(mode="json") for group in groups],
                    "atomic_sources": [
                        self.memories[k].model_dump(mode="json")
                        for k in sorted({k for g in groups for k in g.memory_ids})
                    ],
                    "remaining_sources": [
                        self.turns[k].model_dump(mode="json") for k in sorted(ids)
                    ],
                    "support_check_required": key in self.unsupported,
                    "group_support_verified": key not in self.unsupported,
                    "source_revalidation_failed": key in self.unsupported,
                    **(
                        {"verification_feedback": self.feedback["EXPERIENCE", key]}
                        if ("EXPERIENCE", key) in self.feedback
                        else {}
                    ),
                }
            )
        return values

    def _decide(self):
        candidates = [
            r
            for r in self._records()
            if r["id"] in self.supporting
            and (
                not self.feedback
                or ("EXPERIENCE", r["id"]) in self.feedback
                or ("EXPERIENCE", r["id"]) in self.runner.retry_keys
            )
        ]
        for batch in self.batches(candidates):
            result = self.runner.call("decision", batch, self.semantic.decide, self.spec, batch)
            decisions = result.decisions if result is not None else []
            inputs = {r["id"]: r for r in batch}
            for decision in decisions:
                key = ("application", "EXPERIENCE", decision.id)
                try:
                    self._apply_decision(decision, inputs[decision.id])
                except (ForgettingError, ValueError):
                    self.runner.failures[key] = "INVALID_REPAIR"
                    continue
                self.runner.failures.pop(key, None)

    def _apply_decision(self, decision, record):
        if not record["eligible"] and decision.operation != "KEEP":
            raise ForgettingError("Experience decision exceeds the requested time scope")
        if decision.operation == "DELETE":
            self.current.pop(decision.id, None)
        elif decision.operation in {"REDACT", "REGENERATE"}:
            time = decision.new_valid_time
            allowed = set((record["original_valid_time"] or {}).values()) | {None}
            for source in record["atomic_sources"]:
                allowed.update((source["valid_time"] or {}).values())
            if time and any(v not in allowed for v in time.model_dump().values()):
                raise ForgettingError("An experience repair invented event dates")
            old = self.current.get(decision.id) or self.supporting[decision.id]
            changed = old.model_copy(
                update={
                    "content": decision.new_content,
                    "valid_time": time if time and any(time.model_dump().values()) else None,
                }
            )
            groups = self.runner.call(
                "source_verification",
                [record],
                _supported_groups,
                changed,
                changed.evidence_groups,
                self.memories,
                self.turns,
                self.verifier,
            )
            if groups is None:
                return
            if groups:
                self.current[decision.id] = changed.model_copy(update={"evidence_groups": groups})
                self.unsupported.discard(decision.id)
            else:
                self.current.pop(decision.id, None)

    def _verify(self):
        self.feedback = {}
        for batch in self.batches(self._records()):
            checked = self.runner.call(
                "verification", batch, self.semantic.verify, self.spec, batch
            )
            if checked is None:
                continue
            inputs = {r["id"]: r for r in batch}
            for finding in checked.results:
                if finding.id in self.unsupported and finding.id in self.current:
                    finding = finding.model_copy(update={"supported": False})
                failed = repair_feedback(inputs[finding.id], finding)
                if failed:
                    self.feedback["EXPERIENCE", finding.id] = failed
                else:
                    self.runner.verified(finding.layer, finding.id)

    def _finish(self):
        for key in self.unsupported:
            # An unavailable verifier is uncertainty, not evidence that a claim must
            # be erased. Keep structurally valid sources and report incomplete work.
            if ("source_verification", "EXPERIENCE", key) not in self.runner.failures:
                self.current.pop(key, None)
        failures = [
            VerificationItem(
                layer=layer,
                id=key,
                **{
                    field: value[field]
                    for field in ("target_present", "preservation_ok", "supported")
                },
            )
            for (layer, key), value in self.feedback.items()
        ]
        normalized = {
            key: ExperienceMemory.model_validate(item.model_dump())
            for key, item in self.current.items()
        }
        return ExperienceForgetPlan(normalized, failures, self.passes)
