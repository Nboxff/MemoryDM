import json
from datetime import datetime
from importlib.resources import files

from memory_dm.forgetting.models import (
    Decisions,
    ForgetResponseError,
    ForgetSpec,
    ForgettingError,
    ParsedRequest,
    ReplacementCorrections,
    TargetScopeReview,
    Verification,
)
from memory_dm.llm.clients import LLMClient
from memory_dm.llm.structured import (
    ResponseValidationError,
    StructuredResponseError,
    request_structured,
)
from memory_dm.models import Turn, utc_now


class SemanticForgetter:
    def __init__(self, client: LLMClient):
        self.client = client

    def _call(
        self, prompt: str, payload: dict, schema, name: str, *, validate=None, temperature=None
    ):
        instructions = files("memory_dm.prompts").joinpath(prompt).read_text(encoding="utf-8")
        messages = [
            Turn(session_id="forgetting", role="system", content=instructions),
            Turn(
                session_id="forgetting",
                role="user",
                content=json.dumps(payload, ensure_ascii=False),
            ),
        ]
        try:
            return request_structured(
                self.client, schema, messages, name=name, validate=validate, temperature=temperature
            )
        except StructuredResponseError as error:
            raise ForgetResponseError(
                "Forgetting response failed schema or application validation after one retry"
            ) from error

    def parse(
        self,
        request: str,
        *,
        reference_time: datetime | None = None,
        history: list[Turn] | None = None,
    ) -> ForgetSpec:
        if not request.strip():
            raise ValueError("Forgetting request must not be blank")
        reference_time = reference_time or utc_now()
        if reference_time.tzinfo is None or reference_time.utcoffset() is None:
            raise ValueError("Forgetting reference time must be timezone-aware")
        payload = {
            "request": request,
            "conversation_context": [turn.model_dump(mode="json") for turn in history or []],
            "current_time": reference_time.isoformat(),
            "default_time_field": "RECORDED_TIME",
        }

        def validate(parsed: ParsedRequest) -> None:
            # Semantic time/scope errors need the same repair opportunity as JSON errors.
            if parsed.clarification is None:
                ForgetSpec(original_request=request, **parsed.model_dump(exclude={"clarification"}))
                review = self._call(
                    "forget_scope.md",
                    {
                        **payload,
                        "proposed_target": parsed.model_dump(
                            mode="json", exclude={"clarification"}
                        ),
                    },
                    TargetScopeReview,
                    "forget_scope",
                    temperature=0,
                )
                if not review.matches_request:
                    raise ResponseValidationError(f"Target scope mismatch: {review.feedback}")

        parsed = self._call(
            "forget_parse.md",
            payload,
            ParsedRequest,
            "forget_request",
            validate=validate,
            temperature=0,
        )
        recovered = parsed.clarification is not None
        if recovered:
            # A request for clarification is a fallible interpretation too. Give an
            # independent resolver the original command and context before stopping.
            # Its actionable target must pass the same scope review as a normal parse.
            parsed = self._call(
                "forget_resolve.md",
                {**payload, "previous_interpretation": parsed.model_dump(mode="json")},
                ParsedRequest,
                "forget_resolution",
                validate=validate,
                temperature=0,
            )
        if parsed.clarification is not None:
            raise ForgettingError(
                "Forgetting target is ambiguous; specify the fact, entity or time range"
            )
        return ForgetSpec(
            original_request=request,
            clarification_recovered=recovered,
            **parsed.model_dump(exclude={"clarification"}),
        )

    @staticmethod
    def _check_ids(records: list[dict], items) -> None:
        expected = {(record["layer"], record["id"]) for record in records}
        actual = [(item.layer, item.id) for item in items]
        duplicates = sorted({key for key in actual if actual.count(key) > 1})
        if len(actual) != len(expected) or set(actual) != expected:
            raise ResponseValidationError(
                "Return exactly one item per supplied (layer, id). "
                f"Missing: {sorted(expected - set(actual))}; "
                f"unknown: {sorted(set(actual) - expected)}; duplicates: {duplicates}"
            )

    def decide(self, spec: ForgetSpec, records: list[dict]) -> Decisions:
        def validate(result):
            self._check_ids(records, result.decisions)
            from memory_dm.profile.tokens import PROFILE_TOKEN_LIMIT, token_count

            for item in result.decisions:
                if item.layer == "PROFILE" and item.new_content is not None:
                    if token_count(item.new_content) > PROFILE_TOKEN_LIMIT:
                        raise ResponseValidationError(
                            "The rewritten profile must fit within 1200 cl100k_base tokens; "
                            "aim for 1000 tokens"
                        )

        return self._call(
            "forget_decide.md",
            {"spec": spec.model_dump(mode="json"), "records": records},
            Decisions,
            "forget_decisions",
            validate=validate,
        )

    def verify(self, spec: ForgetSpec, records: list[dict]) -> Verification:
        return self._call(
            "forget_verify.md",
            {"spec": spec.model_dump(mode="json"), "records": records},
            Verification,
            "forget_verification",
            validate=lambda result: self._check_ids(records, result.results),
        )

    def repair_replacements(
        self, spec: ForgetSpec, failures: list[dict], *, reference_time: datetime
    ) -> ReplacementCorrections:
        expected = {failure["index"] for failure in failures}

        def validate(result: ReplacementCorrections) -> None:
            actual = [correction.index for correction in result.corrections]
            if len(actual) != len(expected) or set(actual) != expected:
                raise ResponseValidationError(
                    "Return exactly one correction per failed replacement index. "
                    f"Expected: {sorted(expected)}; received: {actual}"
                )

        return self._call(
            "forget_replace.md",
            {
                "spec": spec.model_dump(mode="json"),
                "reference_time": reference_time.isoformat(),
                "failures": failures,
            },
            ReplacementCorrections,
            "forget_replacement_repair",
            validate=validate,
        )
