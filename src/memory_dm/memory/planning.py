"""Pure reconciliation rules: turn validated decisions into a persistence-independent plan."""

from collections import Counter

from memory_dm.errors import ReconciliationError
from memory_dm.memory.reconciliation import WriteDecision
from memory_dm.memory.state import MemoryLink, WritePlan, WriteSnapshot
from memory_dm.models import AtomicMemory, ValidTime


class MemoryWritePlanner:
    def plan(
        self,
        before: WriteSnapshot,
        candidates: list[AtomicMemory],
        decisions: list[WriteDecision],
        *,
        pending_turn_ids: list[str] | None = None,
    ) -> WritePlan:
        planned = dict(before.memories)
        links = []
        saved_ids = []
        by_id = {memory.id: memory for memory in candidates}
        for decision in decisions:
            candidate = by_id[decision.candidate_id]
            targets = [planned[key] for key in decision.target_memory_ids]
            if decision.operation in ("CORRECT", "SUPERSEDE"):
                candidate_time = max(
                    before.turns[e.turn_id].timestamp
                    for e in candidate.evidence
                    if before.turns[e.turn_id].role == "user"
                )
                for target in targets:
                    target_times = [
                        before.turns[e.turn_id].timestamp
                        for e in target.evidence
                        if before.turns[e.turn_id].role == "user"
                    ]
                    if target_times and max(target_times) > candidate_time:
                        raise ReconciliationError(
                            "An older source cannot replace a newer current fact"
                        )
            if decision.operation == "MERGE":
                canonical = targets[0]
                evidence = self._evidence([*targets, candidate])
                planned[canonical.id] = canonical.model_copy(update={"evidence": evidence})
                for duplicate in targets[1:]:
                    planned[duplicate.id] = duplicate.model_copy(update={"status": "INACTIVE"})
                    links.append(MemoryLink(duplicate.id, canonical.id, "MERGE"))
                saved_ids.append(canonical.id)
            elif decision.operation == "REFINE":
                target = targets[0]
                planned[target.id] = target.model_copy(
                    update={
                        "content": decision.new_content,
                        "evidence": self._evidence([target, candidate]),
                        "valid_time": self._refined_time(target.valid_time, candidate.valid_time),
                    }
                )
                saved_ids.append(target.id)
            else:
                if decision.operation == "CORRECT":
                    candidate = candidate.model_copy(update={"content": decision.new_content})
                planned[candidate.id] = candidate
                saved_ids.append(candidate.id)
                for target in targets:
                    if decision.operation in ("CORRECT", "SUPERSEDE"):
                        status = "INACTIVE" if decision.operation == "CORRECT" else "SUPERSEDED"
                        planned[target.id] = target.model_copy(update={"status": status})
                    links.append(MemoryLink(target.id, candidate.id, decision.operation))
        pending_turn_ids = pending_turn_ids or []
        if any(key not in before.turns or key in before.processed for key in pending_turn_ids):
            raise ReconciliationError(
                "Processing checkpoint refers to unavailable or processed turns"
            )
        return WritePlan(
            upserts=[
                memory for key, memory in planned.items() if memory != before.memories.get(key)
            ],
            links=links,
            processed_turn_ids=pending_turn_ids,
            memory_ids=list(dict.fromkeys(saved_ids)),
            counts=dict(Counter(decision.operation for decision in decisions)),
        )

    @staticmethod
    def _refined_time(old: ValidTime | None, new: ValidTime | None) -> ValidTime | None:
        if new is None or not any((new.start, new.end, new.point)):
            return old
        if old is None or old.point or new.point:
            return new
        # REFINE adds supported detail to the same fact. Preserve an independently
        # evidenced bound when the new message only supplies the other interval bound.
        # An inconsistent combined interval fails validation instead of losing a date.
        return ValidTime(start=new.start or old.start, end=new.end or old.end)

    @staticmethod
    def _evidence(memories):
        return list(
            {
                (e.session_id, e.turn_id, e.span_start, e.span_end): e
                for memory in memories
                for e in memory.evidence
            }.values()
        )
