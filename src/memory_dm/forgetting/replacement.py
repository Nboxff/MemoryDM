"""Stage request-supplied replacement facts without storing the erased request text."""

from dataclasses import dataclass, field
from datetime import datetime

from memory_dm.forgetting.models import (
    ForgetSpec,
    ForgettingError,
    ReplacementFact,
    Verification,
)
from memory_dm.forgetting.semantic import SemanticForgetter
from memory_dm.models import AtomicMemory, Evidence, Session, Turn
from memory_dm.storage.sqlite import SQLiteStore


@dataclass
class ReplacementPlan:
    session: Session | None = None
    turns: list[Turn] = field(default_factory=list)
    memories: list[AtomicMemory] = field(default_factory=list)


def prepare_replacements(
    spec: ForgetSpec,
    user_id: str,
    store: SQLiteStore,
    semantic: SemanticForgetter,
    *,
    reference_time: datetime,
    session_id: str | None,
) -> ReplacementPlan:
    """Independently check grounding and target absence before any persistent change.

    A source turn is a redacted projection of the user's request: only the verified new
    claim survives. The full request is evidence for verification in this call only.
    """
    if not spec.replacements:
        return ReplacementPlan()
    if reference_time.tzinfo is None or reference_time.utcoffset() is None:
        raise ValueError("Forgetting reference time must be timezone-aware")
    if session_id is not None:
        session = store.get_session(session_id, user_id)
        if session is None:
            raise ForgettingError("Replacement session does not belong to this user")
        new_session = None
    else:
        session = Session(user_id=user_id, created_at=reference_time)
        new_session = session

    facts = list({fact.model_dump_json(): fact for fact in spec.replacements}.values())
    for attempt in range(2):
        current_spec = spec.model_copy(update={"replacements": facts})
        plan, records = _stage_replacements(current_spec, session, new_session, reference_time)
        result = semantic.verify(current_spec, records)
        failures = _replacement_failures(facts, plan, result)
        if not failures:
            return plan
        if attempt == 0:
            repaired = semantic.repair_replacements(
                current_spec, failures, reference_time=reference_time
            )
            # Passing replacements are unchanged; a correction cannot drop or add an index.
            facts = list(facts)
            for correction in repaired.corrections:
                facts[correction.index] = correction.fact

    checks = set()
    for failure in failures:
        for finding in failure["findings"]:
            if finding["target_present"]:
                checks.add("target_present")
            if not finding["preservation_ok"]:
                checks.add("preservation_ok")
            if not finding["supported"]:
                checks.add("supported")
    raise ForgettingError(
        "Replacement verification failed after one semantic repair "
        f"({', '.join(sorted(checks))}); no changes were committed"
    )


def _replacement_failures(
    facts: list[ReplacementFact], plan: ReplacementPlan, result: Verification
) -> list[dict]:
    findings = {(item.layer, item.id): item for item in result.results}
    failures = []
    for index, (fact, source, memory) in enumerate(
        zip(facts, plan.turns, plan.memories, strict=True)
    ):
        rejected = [
            item
            for item in (findings[("RAW", source.turn_id)], findings[("ATOMIC", memory.id)])
            if item.target_present or not item.preservation_ok or not item.supported
        ]
        if rejected:
            failures.append(
                {
                    "index": index,
                    "fact": fact.model_dump(mode="json"),
                    "findings": [item.model_dump(mode="json") for item in rejected],
                }
            )
    return failures


def _stage_replacements(
    spec: ForgetSpec,
    session: Session,
    new_session: Session | None,
    reference_time: datetime,
) -> tuple[ReplacementPlan, list[dict]]:
    plan = ReplacementPlan(session=new_session)

    records = []
    for fact in spec.replacements:
        source = Turn(
            session_id=session.session_id,
            role="user",
            content=fact.content,
            timestamp=reference_time,
        )
        memory = AtomicMemory(
            user_id=session.user_id,
            content=fact.content,
            type=fact.type,
            valid_time=fact.valid_time.model_dump() if fact.valid_time else None,
            evidence=[Evidence(session_id=session.session_id, turn_id=source.turn_id)],
        )
        original = source.model_copy(update={"content": spec.original_request})
        base = {
            "eligible": True,
            "replacement_check": True,
            "content": fact.content,
            "original_valid_time": None,
            "valid_time": fact.valid_time.model_dump() if fact.valid_time else None,
        }
        records.extend(
            [
                {
                    **base,
                    "layer": "RAW",
                    "id": source.turn_id,
                    "original_content": spec.original_request,
                    "valid_time": None,
                    "role": "user",
                    "timestamp": reference_time.isoformat(),
                    "context": [],
                },
                {
                    **base,
                    "layer": "ATOMIC",
                    "id": memory.id,
                    "original_content": None,
                    "support_check_required": True,
                    "remaining_sources": [original.model_dump(mode="json")],
                    "created_at": memory.created_at.isoformat(),
                    "status": memory.status,
                },
            ]
        )
        plan.turns.append(source)
        plan.memories.append(memory)

    return plan, records
