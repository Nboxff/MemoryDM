"""Semantic intent and topic routing, followed by independent write scheduling."""

import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Literal

from pydantic import Field

from memory_dm.llm.clients import LLMClient, LLMModelView
from memory_dm.llm.structured import StructuredResponseError, request_structured
from memory_dm.models import Record, Turn

MemoryAction = Literal["NONE", "WRITE", "FORGET"]


class RoutingDecision(Record):
    action: MemoryAction
    topic_switch: bool = Field(default=False, strict=True)
    possible_forgetting: bool = Field(default=False, strict=True)


@dataclass(frozen=True)
class RoutingResult:
    action: MemoryAction
    intent: MemoryAction
    topic_switch: bool
    initial_action: MemoryAction
    review_action: MemoryAction | None = None


class RoutingError(ValueError):
    """No validated routing decision is available; do not persist the request as chat."""


class MemoryRouter:
    def __init__(
        self, client: LLMClient, *, write_every: int = 5, classifier_model: str | None = None
    ):
        self.client, self.write_every = client, write_every
        self.classifier = LLMModelView(client, classifier_model) if classifier_model else client

    def _classify(self, prompt_name: str, payload: dict) -> RoutingDecision:
        # Routing has its own authority rules, unrelated to retrieval subject resolution.
        prompt = (
            files("memory_dm.prompts").joinpath(prompt_name + ".md").read_text(encoding="utf-8")
        )
        try:
            return request_structured(
                self.classifier if prompt_name == "route_memory" else self.client,
                RoutingDecision,
                [
                    Turn(session_id=prompt_name, role="system", content=prompt),
                    Turn(session_id=prompt_name, role="user", content=json.dumps(payload)),
                ],
                name=prompt_name,
                temperature=0,
            )
        except StructuredResponseError as error:
            raise RoutingError(f"Invalid structured response for {prompt_name}") from error

    def route(self, content: str, history: list[Turn], pending_users: int) -> RoutingResult:
        payload = {
            "message": content,
            "conversation_context": [turn.model_dump(mode="json") for turn in history[-6:]],
        }
        initial = decision = self._classify("route_memory", payload)
        review = None
        if decision.action != "FORGET" and decision.possible_forgetting:
            decision = self._classify(
                "review_memory_route",
                {**payload, "previous_decision": initial.model_dump(mode="json")},
            )
            review = decision.action
            if decision.action != "FORGET" and decision.possible_forgetting:
                # One review exhausted the uncertainty check. Continue as ordinary
                # dialogue; write scheduling below remains independent of intent.
                decision = decision.model_copy(
                    update={"action": "NONE", "possible_forgetting": False}
                )
        # Scheduling runs AFTER deletion review. A scheduled WRITE is not evidence
        # that the user asked to store anything, and never overrides genuine FORGET.
        action = decision.action
        if action != "FORGET" and (
            action == "WRITE"
            or pending_users >= self.write_every
            or (pending_users > 0 and decision.topic_switch)
        ):
            action = "WRITE"
        return RoutingResult(
            action=action,
            intent=decision.action,
            topic_switch=decision.topic_switch,
            initial_action=initial.action,
            review_action=review,
        )
