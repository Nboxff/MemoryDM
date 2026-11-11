"""Batch write decisions and validation against the candidates actually retrieved."""

import json
from importlib.resources import files
from typing import Literal

from pydantic import model_validator
from pydantic_core import PydanticCustomError

from memory_dm.errors import ReconciliationError as ReconciliationError
from memory_dm.llm.clients import LLMClient
from memory_dm.llm.references import References
from memory_dm.llm.structured import (
    ResponseValidationError,
    StructuredResponseError,
    request_structured,
)
from memory_dm.models import AtomicMemory, Record, Turn

MemoryOperation = Literal["ADD", "MERGE", "REFINE", "CORRECT", "SUPERSEDE", "KEEP_BOTH"]


class WriteDecision(Record):
    operation: MemoryOperation
    candidate_id: str
    target_memory_ids: list[str]
    new_content: str | None

    @model_validator(mode="after")
    def check_operation(self):
        if len(set(self.target_memory_ids)) != len(self.target_memory_ids):
            raise PydanticCustomError("duplicate_write_targets", "Write targets must be unique")
        if self.operation == "ADD":
            if self.target_memory_ids:
                raise PydanticCustomError("add_has_targets", "ADD cannot target existing memories")
        elif not self.target_memory_ids:
            raise PydanticCustomError(
                "missing_write_target", "This operation requires an existing target"
            )
        if self.operation == "REFINE" and len(self.target_memory_ids) != 1:
            raise PydanticCustomError(
                "refine_target_count", "REFINE requires one independently manageable target"
            )
        if self.operation in ("REFINE", "CORRECT"):
            if self.new_content is None:
                raise PydanticCustomError(
                    "missing_replacement_text", "REFINE and CORRECT require replacement text"
                )
        elif self.new_content is not None:
            raise PydanticCustomError(
                "unexpected_replacement_text", "This operation cannot replace text"
            )
        return self


class WriteDecisions(Record):
    decisions: list[WriteDecision]


class WriteDecisionValidator:
    def validate(
        self, candidates: list[AtomicMemory], related: dict, decisions: list[WriteDecision]
    ):
        ids = {memory.id for memory in candidates}
        if len(ids) != len(candidates):
            raise ReconciliationError(
                "Candidate IDs must be unique",
                diagnostics={"issues": [{"code": "duplicate_candidates"}]},
            )
        actual = [decision.candidate_id for decision in decisions]
        if len(actual) != len(ids) or set(actual) != ids:
            raise ReconciliationError(
                "Each candidate must have exactly one write decision",
                diagnostics={
                    "issues": [
                        {
                            "code": "candidate_coverage",
                            "missing_ids": sorted(ids - set(actual)),
                            "unknown_id_count": len(set(actual) - ids),
                            "duplicate_ids": sorted(
                                {key for key in actual if key in ids and actual.count(key) > 1}
                            ),
                        }
                    ]
                },
            )
        used = {}
        for decision in decisions:
            permitted = {memory.id: memory for memory in related[decision.candidate_id]}
            candidate = next(memory for memory in candidates if memory.id == decision.candidate_id)
            for target_id in decision.target_memory_ids:
                target = permitted.get(target_id)
                if (
                    target is None
                    or target.user_id != candidate.user_id
                    or target.status != "ACTIVE"
                ):
                    raise ReconciliationError(
                        "Write target is unavailable or outside retrieved candidates",
                        diagnostics={
                            "issues": [
                                {
                                    "code": "unavailable_target",
                                    "candidate_ids": [candidate.id],
                                    "target_ids": [target_id] if target_id in permitted else [],
                                }
                            ]
                        },
                    )
                previous = used.get(target_id)
                if previous is not None and not (
                    previous.operation == decision.operation == "MERGE"
                    and previous.target_memory_ids[0] == decision.target_memory_ids[0]
                ):
                    raise ReconciliationError(
                        f"Conflicting writes target the same memory {target_id}: "
                        f"candidate {previous.candidate_id} ({previous.operation}) and "
                        f"candidate {decision.candidate_id} ({decision.operation}); "
                        "replan target allocation across the complete batch",
                        diagnostics={
                            "issues": [
                                {
                                    "code": "conflicting_targets",
                                    "candidate_ids": [previous.candidate_id, decision.candidate_id],
                                    "target_ids": [target_id],
                                }
                            ]
                        },
                    )
                used[target_id] = decision
        return decisions


class BatchReconciler:
    def __init__(self, client: LLMClient):
        self.client = client
        self.validator = WriteDecisionValidator()

    def reconcile(
        self,
        candidates: list[AtomicMemory],
        related: dict,
        turns: list[Turn],
        *,
        existing_sources: dict[str, Turn] | None = None,
    ):
        if not candidates:
            return []
        candidate_ids = [c.id for c in candidates]
        target_ids = [m.id for values in related.values() for m in values]
        candidate_refs = References(candidate_ids, "c", reserved=target_ids)
        target_refs = References(target_ids, "m", reserved=candidate_ids)
        prompt = files("memory_dm.prompts").joinpath("reconcile.md").read_text(encoding="utf-8")
        payload = {
            "new_memories": [
                {**memory.model_dump(mode="json"), "id": candidate_refs.encode(memory.id)}
                for memory in candidates
            ],
            "related_memories": {
                candidate_refs.encode(key): [
                    {**memory.model_dump(mode="json"), "id": target_refs.encode(memory.id)}
                    for memory in values
                ]
                for key, values in related.items()
            },
            "allowed_target_ids": {
                candidate_refs.encode(key): [target_refs.encode(m.id) for m in values]
                for key, values in related.items()
            },
            "conversation_context": [turn.model_dump(mode="json") for turn in turns],
            "existing_sources": [
                turn.model_dump(mode="json") for turn in (existing_sources or {}).values()
            ],
        }
        messages = [
            Turn(session_id="reconciliation", role="system", content=prompt),
            Turn(
                session_id="reconciliation",
                role="user",
                content=json.dumps(payload, ensure_ascii=False),
            ),
        ]

        def decode(result):
            return [
                d.model_copy(
                    update={
                        "candidate_id": candidate_refs.decode(d.candidate_id),
                        "target_memory_ids": [
                            target_refs.decode(key) for key in d.target_memory_ids
                        ],
                    }
                )
                for d in result.decisions
            ]

        def validate(result: WriteDecisions) -> None:
            try:
                self.validator.validate(candidates, related, decode(result))
            except ReconciliationError as error:
                feedback = str(error)
                for refs in (candidate_refs, target_refs):
                    for key, alias in refs.encoded.items():
                        feedback = feedback.replace(key, alias)
                feedback += (
                    "\nAllowed target IDs per candidate: "
                    + json.dumps(payload["allowed_target_ids"])
                    + "\nReturn a globally consistent batch; never target a new candidate."
                )
                raise ResponseValidationError(
                    feedback, issues=error.diagnostics["issues"]
                ) from error

        try:
            result = request_structured(
                self.client,
                WriteDecisions,
                messages,
                name="memory_reconciliation",
                validate=validate,
            )
        except StructuredResponseError as error:
            raise ReconciliationError(
                "Write decisions failed schema or batch validation after one retry",
                diagnostics=error.diagnostics,
            ) from error
        return decode(result)
