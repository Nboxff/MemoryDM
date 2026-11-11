"""Extract atomic memories with one validation repair and per-memory recovery."""

import json
import logging
import re
from dataclasses import dataclass
from importlib.resources import files

from pydantic import Field, ValidationError

from memory_dm.llm.clients import ClientError, LLMClient
from memory_dm.llm.structured import (
    ResponseValidationError,
    StructuredResponseError,
    request_validated_json,
    structured_schema,
)
from memory_dm.memory.validator import MemoryValidator
from memory_dm.models import AtomicMemory, Evidence, MemoryType, Record, Session, Turn, ValidTime

logger = logging.getLogger(__name__)


class ExtractionEvidence(Evidence):
    span_start: int | None = Field(ge=0, strict=True)
    span_end: int | None = Field(ge=0, strict=True)


class ExtractionTime(ValidTime):
    start: str | None
    end: str | None
    point: str | None


class MemoryCandidate(Record):
    content: str
    type: MemoryType
    valid_time: ExtractionTime | None
    evidence: list[ExtractionEvidence] = Field(min_length=1)


class ExtractionResult(Record):
    memories: list[MemoryCandidate]


class _ExtractionEnvelope(Record):
    """Validate the response container independently of individual memories."""

    memories: list[object]


@dataclass
class _ExtractionAttempt:
    memories: list[AtomicMemory]
    errors: list[str]


class AtomicMemoryExtractor:
    def __init__(self, client: LLMClient, *, output_language: str = "auto"):
        if not output_language.strip():
            raise ValueError("Memory output language must not be blank")
        self.client = client
        self.output_language = output_language.strip()
        self.validator = MemoryValidator()

    def extract(
        self, session: Session, turns: list[Turn], *, new_turn_ids: list[str] | None = None
    ) -> list[AtomicMemory]:
        # Input Validation
        if any(turn.session_id != session.session_id for turn in turns):
            raise ValueError("Extraction turns must belong to the requested session")
        if len({turn.turn_id for turn in turns}) != len(turns):
            raise ValueError("Extraction turn IDs must be unique")
        if new_turn_ids is not None and not set(new_turn_ids).issubset(
            {turn.turn_id for turn in turns}
        ):
            raise ValueError("New turn IDs must be present in the extraction context")
        if not any(turn.role == "user" for turn in turns):
            return []

        # Load prompt template
        template = files("memory_dm.prompts").joinpath("extract.md").read_text(encoding="utf-8")
        instructions, _, data_template = template.partition("# Conversation\n")

        conversation = json.dumps(
            [turn.model_dump(mode="json") for turn in turns], ensure_ascii=False
        )
        metadata_values = {
            **session.model_dump(mode="json"),
            "output_language": self.output_language,
        }
        if new_turn_ids is not None:
            metadata_values["new_turn_ids"] = new_turn_ids
        metadata = json.dumps(metadata_values, ensure_ascii=False)
        values = {"{{conversation}}": conversation, "{{session_metadata}}": metadata}
        data = "# Conversation\n" + re.sub(
            r"\{\{(?:conversation|session_metadata)\}\}",
            lambda match: values[match.group()],
            data_template,
        )
        messages = [
            Turn(session_id=session.session_id, role="system", content=instructions),
            Turn(session_id=session.session_id, role="user", content=data),
        ]

        memories = self._extract_with_retry(messages, session, turns)
        memories = self.validator.validate_batch(memories, turns)
        if new_turn_ids is not None:
            fresh = {
                turn.turn_id
                for turn in turns
                if turn.turn_id in new_turn_ids and turn.role == "user"
            }
            memories = [
                memory for memory in memories if any(e.turn_id in fresh for e in memory.evidence)
            ]
        return memories

    def _extract_with_retry(
        self, messages: list[Turn], session: Session, turns: list[Turn]
    ) -> list[AtomicMemory]:
        attempts = []

        def parse(content: str) -> list[AtomicMemory]:
            result = self._parse_response(content, session, turns)
            attempts.append(result)
            if result.errors:
                raise ResponseValidationError("\n".join(result.errors))
            return result.memories

        try:
            return request_validated_json(
                self.client,
                messages,
                schema=structured_schema(ExtractionResult),
                name="atomic_memory_extraction",
                parse=parse,
            )
        except ClientError:
            if not attempts or not attempts[0].memories:
                raise
            logger.warning(
                "Extraction retry request failed; retaining %d validated memories",
                len(attempts[0].memories),
                exc_info=True,
            )
            return attempts[0].memories
        except StructuredResponseError as error:
            # Recovery is specific to extraction; mutation plans require complete validation.
            memories = [memory for attempt in attempts for memory in attempt.memories]
            if not memories:
                raise ValueError(
                    "Extraction response does not match the memory schema after one retry"
                ) from error
            logger.warning("Extraction repair still failed validation; retaining valid memories")
            return memories

    def _parse_response(
        self, content: str, session: Session, turns: list[Turn]
    ) -> _ExtractionAttempt:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as error:
            return _ExtractionAttempt([], [f"Invalid JSON: {error}"])

        errors = []
        try:
            candidates = _ExtractionEnvelope.model_validate(payload).memories
        except ValidationError as error:
            errors.append(f"response: {error.json(include_url=False, include_input=False)}")
            # Even an invalid envelope can contain independently valid memories.
            candidates = payload.get("memories") if isinstance(payload, dict) else None
            if not isinstance(candidates, list):
                return _ExtractionAttempt([], errors)

        memories = []
        user_turn_ids = {turn.turn_id for turn in turns if turn.role == "user"}
        for index, raw in enumerate(candidates):
            try:
                candidate = MemoryCandidate.model_validate(raw)
                memory = AtomicMemory(user_id=session.user_id, **candidate.model_dump())
                memory = self.validator.validate(memory, turns)
                if not any(e.turn_id in user_turn_ids for e in memory.evidence):
                    raise ValueError("New memories require a supporting user message")
                memories.append(memory)
            except ValidationError as error:
                errors.append(
                    f"memories[{index}]: {error.json(include_url=False, include_input=False)}"
                )
            except ValueError as error:
                errors.append(f"memories[{index}]: {error}")
        return _ExtractionAttempt(memories, errors)
