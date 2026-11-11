"""Bounded experience proposals and independent per-group support checks."""

import json
from importlib.resources import files

from memory_dm.experience.models import ExperienceCandidates, ExperienceVerification
from memory_dm.llm.clients import LLMClient
from memory_dm.llm.structured import ResponseValidationError, request_structured
from memory_dm.models import Turn


class ExperienceSemantic:
    def __init__(self, client: LLMClient):
        self.client = client

    def _call(self, name, model, payload, validate):
        prompt = files("memory_dm.prompts").joinpath(name + ".md").read_text(encoding="utf-8")
        return request_structured(
            self.client,
            model,
            [
                Turn(session_id=name, role="system", content=prompt),
                Turn(session_id=name, role="user", content=json.dumps(payload, ensure_ascii=False)),
            ],
            name=name,
            validate=validate,
            temperature=0,
        )

    def propose(self, payload: dict, memories: dict, user_id: str) -> ExperienceCandidates:
        # A well-formed proposal may contain unsupported inferences. The service
        # rejects these locally; one bad claim must not invalidate independent claims.
        return self._call("experience_extract", ExperienceCandidates, payload, None)

    def verify(self, payload: dict) -> ExperienceVerification:
        expected = {
            (i, j)
            for i, candidate in enumerate(payload["candidates"])
            for j, _ in enumerate(candidate["evidence_groups"])
        }

        def validate(result: ExperienceVerification) -> None:
            actual = [(r.candidate_index, r.group_index) for r in result.results]
            if len(actual) != len(expected) or set(actual) != expected:
                raise ResponseValidationError(
                    "Return exactly one support check for every supplied candidate/group index"
                )

        return self._call("experience_verify", ExperienceVerification, payload, validate)
