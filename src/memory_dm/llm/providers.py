"""Explicit wire-protocol strategies; model aliases and endpoint hosts are never guessed.

Register another RequestAdapter to support a new service without changing LLMClient.
"""

import warnings
from dataclasses import dataclass
from typing import Protocol

from memory_dm.config import APIConfig


class RequestAdapter(Protocol):
    def adapt(self, config: APIConfig, options: dict) -> dict:
        """Return SDK options without mutating the caller's dictionary."""
        ...


@dataclass(frozen=True)
class ChatCompletionsAdapter:
    token_parameter: str = "max_tokens"
    json_modes: tuple[str, ...] = ("json_object", "json_schema")
    default_thinking: bool | None = None

    def thinking_options(self, enabled: bool | None, effort: str | None) -> dict:
        """Legacy compatible protocol; subclasses supply their own wire representation."""
        options = {}
        if enabled is not None:
            options["extra_body"] = {"thinking": {"type": "enabled" if enabled else "disabled"}}
        if effort not in (None, "none"):
            options["reasoning_effort"] = effort
        return options

    def unsupported_parameters(self, enabled: bool | None) -> tuple[str, ...]:
        return ()

    def adapt(self, config: APIConfig, options: dict) -> dict:
        result = dict(options)
        output_tokens = result.pop("max_output_tokens", None)
        if output_tokens is not None:
            result[self.token_parameter] = output_tokens
        response_format = result.get("response_format")
        if response_format and response_format.get("type") not in self.json_modes:
            raise ValueError(f"{config.provider} does not support this JSON mode")
        enabled = config.thinking_enabled
        effort = config.reasoning_effort
        if enabled is False and effort not in (None, "none"):
            raise ValueError("Thinking is disabled but reasoning_effort requests reasoning")
        if enabled is True and effort == "none":
            raise ValueError("Thinking is enabled but reasoning_effort is none")
        if enabled is None:
            enabled = effort != "none" if effort is not None else self.default_thinking
        result.update(self.thinking_options(enabled, effort))
        for name in self.unsupported_parameters(enabled):
            if name in result:
                result.pop(name)
                warnings.warn(
                    f"{config.provider}: {name} omitted while thinking is enabled",
                    UserWarning,
                    stacklevel=3,
                )
        return result


@dataclass(frozen=True)
class DeepSeekAdapter(ChatCompletionsAdapter):
    default_thinking: bool | None = False
    json_modes: tuple[str, ...] = ("json_object",)

    def unsupported_parameters(self, enabled):
        return ("temperature", "presence_penalty", "frequency_penalty") if enabled else ()


@dataclass(frozen=True)
class OpenAIAdapter(ChatCompletionsAdapter):
    token_parameter: str = "max_completion_tokens"
    default_thinking: bool | None = False

    def thinking_options(self, enabled, effort):
        return {"reasoning_effort": (effort or "medium") if enabled else "none"}

    def unsupported_parameters(self, enabled):
        return ("temperature", "top_p", "presence_penalty", "frequency_penalty") if enabled else ()


@dataclass(frozen=True)
class NonReasoningOpenAIAdapter(ChatCompletionsAdapter):
    token_parameter: str = "max_completion_tokens"
    default_thinking: bool | None = False

    def thinking_options(self, enabled, effort):
        if enabled or effort not in (None, "none"):
            raise ValueError("This adapter is for models without a reasoning mode")
        return {}


@dataclass(frozen=True)
class QwenAdapter(ChatCompletionsAdapter):
    default_thinking: bool | None = False

    def thinking_options(self, enabled, effort):
        if effort not in (None, "none"):
            raise ValueError("Qwen adapter uses thinking_enabled, not reasoning_effort")
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": enabled}}}


@dataclass(frozen=True)
class DashScopeAdapter(QwenAdapter):
    def thinking_options(self, enabled, effort):
        super().thinking_options(enabled, effort)
        return {"extra_body": {"enable_thinking": enabled}}


_ADAPTERS: dict[str, RequestAdapter] = {}


def register_adapter(name: str, adapter: RequestAdapter) -> None:
    """Register at application startup; duplicate names fail rather than changing behavior."""
    if not name.strip() or name in _ADAPTERS:
        raise ValueError(f"Invalid or already registered adapter: {name}")
    _ADAPTERS[name] = adapter


def get_adapter(name: str) -> RequestAdapter:
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise ValueError(f"Unknown LLM provider {name!r}; choose {', '.join(_ADAPTERS)}") from None


register_adapter("compatible", ChatCompletionsAdapter())
register_adapter("deepseek", DeepSeekAdapter())
register_adapter("openai", OpenAIAdapter())
register_adapter("openai_chat", NonReasoningOpenAIAdapter())
register_adapter("qwen", QwenAdapter())
register_adapter("qwen_dashscope", DashScopeAdapter())
