"""Chat interface and JSON parser extracted from Bemula."""

from __future__ import annotations

import json
import re
from typing import Any

Message = dict[str, str]

DEFAULT_MAX_TOKENS = 16384

class BaseLLM:
    """Minimal chat interface used across the generation pipeline."""

    def chat(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.9,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        task: str = "generic",
    ) -> str:
        raise NotImplementedError

    def chat_json(
        self,
        messages: list[Message],
        *,
        temperature: float = 0.9,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        task: str = "generic",
    ) -> Any:
        """Chat and parse the response as JSON (array or object)."""
        text = self.chat(messages, temperature=temperature, max_tokens=max_tokens, task=task)
        return parse_json(text)

def parse_json(text: str) -> Any:
    """Extract the first JSON array/object from an LLM response."""
    match = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON found in LLM response: {text[:200]!r}")
    return json.loads(match.group(1))
