"""Bounded structured-response repair, independent of each caller's recovery policy."""

import json
from collections.abc import Callable
from importlib.resources import files

from pydantic import BaseModel, ValidationError

from memory_dm.llm.clients import LLMClient
from memory_dm.models import Turn


class ResponseValidationError(ValueError):
    """A model response failed a schema or application contract; safe to feed back."""

    def __init__(self, message: str, *, issues: list[dict] | None = None):
        super().__init__(message)
        self.issues = issues or [{"code": "application_validation"}]


class StructuredResponseError(ValueError):
    """Both responses failed validation. The public message contains no model text."""

    def __init__(self, message: str, *, diagnostics: dict | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def structured_schema(model: type[BaseModel]) -> dict:
    """Require all JSON fields while retaining ergonomic defaults for Python callers."""
    schema = model.model_json_schema()

    def visit(value):
        if isinstance(value, dict):
            value.pop("default", None)
            if value.get("type") == "object" and "properties" in value:
                value["required"] = list(value["properties"])
                value["additionalProperties"] = False
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(schema)
    return schema


def request_validated_json[T](
    client: LLMClient,
    messages: list[Turn],
    *,
    schema: dict,
    name: str,
    parse: Callable[[str], T],
    temperature: float | None = None,
) -> T:
    """Retry validation once with the response and errors; never retry transport here."""
    known_fields = set()

    def collect_fields(value):
        if isinstance(value, dict):
            known_fields.update(value.get("properties", {}))
            for child in value.values():
                collect_fields(child)
        elif isinstance(value, list):
            for child in value:
                collect_fields(child)

    collect_fields(schema)
    instructions = list(messages)
    if client.config.json_mode == "json_object":
        instructions[0] = instructions[0].model_copy(
            update={
                "content": instructions[0].content
                + "\n\n# Required JSON schema\n\n"
                + json.dumps(schema, ensure_ascii=False)
            }
        )
    for attempt in range(2):
        options = {"temperature": temperature} if temperature is not None else {}
        content = client.chat_json(instructions, schema=schema, name=name, **options)
        try:
            return parse(content)
        except ValidationError as error:
            feedback = error.json(include_url=False, include_input=False, include_context=False)
            issues = [
                {
                    "code": item["type"],
                    "path": [
                        part if isinstance(part, int) or part in known_fields else "<unknown_field>"
                        for part in item["loc"]
                    ],
                }
                for item in error.errors(include_input=False, include_context=False)
            ]
        except ResponseValidationError as error:
            feedback = str(error)
            issues = error.issues
        if attempt:
            raise StructuredResponseError(
                f"Invalid structured response for {name} after one retry",
                diagnostics={"operation": name, "attempts": 2, "issues": issues},
            )
        repair = files("memory_dm.prompts").joinpath("repair_json.md").read_text(encoding="utf-8")
        instructions = [
            *instructions,
            Turn(session_id=name, role="assistant", content=content),
            Turn(
                session_id=name,
                role="user",
                content=repair.replace("{{validation_errors}}", feedback),
            ),
        ]
    raise AssertionError("Unreachable structured request state")


def request_structured[Model: BaseModel](
    client: LLMClient,
    model: type[Model],
    messages: list[Turn],
    *,
    name: str,
    validate: Callable[[Model], None] | None = None,
    temperature: float | None = None,
) -> Model:
    def parse(content: str) -> Model:
        result = model.model_validate_json(content)
        if validate is not None:
            validate(result)
        return result

    return request_validated_json(
        client,
        messages,
        schema=structured_schema(model),
        name=name,
        parse=parse,
        temperature=temperature,
    )
