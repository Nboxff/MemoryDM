"""Load local configuration without changing the process environment."""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values


@dataclass(frozen=True)
class APIConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout: float = 30.0
    max_retries: int = 2
    json_mode: Literal["json_object", "json_schema"] = "json_object"
    max_concurrency: int = 4
    batch_size: int = 64
    thinking_enabled: bool | None = None
    provider: str = "compatible"
    reasoning_effort: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None
    seed: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("API provider must be a nonblank adapter name")
        if self.reasoning_effort not in (
            None,
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        ):
            raise ValueError("Unknown reasoning effort")
        for name, lower, upper in (
            ("temperature", 0, 2),
            ("top_p", 0, 1),
            ("presence_penalty", -2, 2),
            ("frequency_penalty", -2, 2),
        ):
            value = getattr(self, name)
            if value is not None and (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not lower <= value <= upper
            ):
                raise ValueError(f"{name} must be finite and between {lower} and {upper}")
        if self.max_output_tokens is not None and (
            type(self.max_output_tokens) is not int or self.max_output_tokens <= 0
        ):
            raise ValueError("max_output_tokens must be a positive integer")
        if self.seed is not None and type(self.seed) is not int:
            raise ValueError("seed must be an integer")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("API_TIMEOUT must be a finite positive number")
        if self.max_retries < 0:
            raise ValueError("API_MAX_RETRIES must be non-negative")
        if self.json_mode not in ("json_object", "json_schema"):
            raise ValueError("JSON mode must be json_object or json_schema")
        if self.thinking_enabled is not None and type(self.thinking_enabled) is not bool:
            raise ValueError("Thinking mode must be a boolean or None")
        for name, value in (
            ("max_concurrency", self.max_concurrency),
            ("batch_size", self.batch_size),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class ForgettingOptions:
    """Options for the retrieval-based forgetting pipeline."""

    strategy: Literal["minimal"] = "minimal"
    local_rewrite: bool = True
    source_revalidation: bool = True
    regenerate: bool = True

    def __post_init__(self):
        if self.strategy != "minimal":
            raise ValueError("Unknown forgetting strategy")
        if any(
            type(v) is not bool
            for v in (self.local_rewrite, self.source_revalidation, self.regenerate)
        ):
            raise ValueError("Forgetting ablations must be booleans")


@dataclass(frozen=True)
class Settings:
    db_path: Path
    user_id: str
    llm: APIConfig
    embedding: APIConfig
    output_language: str = "auto"
    forget_batch_size: int = 16
    forget_batch_chars: int = 24000
    forget_max_passes: int = 2
    auto_memory: bool = True
    memory_write_every: int = 5
    memory_context_turns: int = 12
    memory_top_k: int = 10
    memory_context_chars: int = 16000
    retrieval_context_tokens: int = 16384
    retrieval_max_rounds: int = 2
    retrieval_neighbor_turns: int = 2
    show_metrics: bool = False
    experience_enabled: bool = True
    experience_batch_size: int = 16
    experience_retrieval_enabled: bool = True
    profile_enabled: bool = True
    memory_write_tokens: int = 0
    forget_probe_count: int = 0
    forgetting: ForgettingOptions = field(default_factory=ForgettingOptions)
    retrieval_methods: tuple[str, ...] = ("hybrid", "grep", "time")
    router_model: str | None = None

    def __post_init__(self) -> None:
        for value in (self.forget_batch_size, self.forget_batch_chars, self.forget_max_passes):
            if type(value) is not int or value <= 0:
                raise ValueError("Forgetting limits must be positive integers")
        for value in (
            self.memory_write_every,
            self.memory_context_turns,
            self.memory_top_k,
            self.memory_context_chars,
            self.retrieval_context_tokens,
            self.retrieval_neighbor_turns,
            self.experience_batch_size,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("Memory limits must be positive integers")
        if type(self.auto_memory) is not bool:
            raise ValueError("Automatic memory must be a boolean")
        if type(self.retrieval_max_rounds) is not int or not 1 <= self.retrieval_max_rounds <= 2:
            raise ValueError("Retrieval rounds must be 1 or 2")
        if type(self.show_metrics) is not bool:
            raise ValueError("Metrics display must be a boolean")
        if type(self.experience_enabled) is not bool:
            raise ValueError("Automatic experience consolidation must be a boolean")
        for value in (self.experience_retrieval_enabled, self.profile_enabled):
            if type(value) is not bool:
                raise ValueError("Derived layer switches must be booleans")
        for value in (self.memory_write_tokens, self.forget_probe_count):
            if type(value) is not int or value < 0:
                raise ValueError("Optional maintenance and probe limits must be non-negative")
        if self.forget_probe_count > 5:
            raise ValueError("At most five runtime forgetting probes are allowed")
        if not self.retrieval_methods or set(self.retrieval_methods) - {"hybrid", "grep", "time"}:
            raise ValueError("Retrieval methods must be a nonempty subset of hybrid, grep and time")
        if len(set(self.retrieval_methods)) != len(self.retrieval_methods):
            raise ValueError("Retrieval methods cannot be repeated")
        if self.router_model is not None and (
            not isinstance(self.router_model, str) or not self.router_model.strip()
        ):
            raise ValueError("Router model must be a nonblank name or None")

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "Settings":
        values = {**dotenv_values(env_file), **os.environ}
        timeout = float(values.get("API_TIMEOUT") or "30")
        max_retries = int(values.get("API_MAX_RETRIES") or "2")
        auto_memory = (values.get("MEMORY_DM_AUTO_MEMORY") or "true").strip().lower()
        if auto_memory not in ("true", "false"):
            raise ValueError("MEMORY_DM_AUTO_MEMORY must be true or false")
        show_metrics = (values.get("MEMORY_DM_SHOW_METRICS") or "false").strip().lower()
        if show_metrics not in ("true", "false"):
            raise ValueError("MEMORY_DM_SHOW_METRICS must be true or false")
        experience = (values.get("EXPERIENCE_ENABLED") or "true").strip().lower()
        if experience not in ("true", "false"):
            raise ValueError("EXPERIENCE_ENABLED must be true or false")

        def switch(name: str, default=False) -> bool:
            value = (values.get(name) or str(default)).strip().lower()
            if value not in ("true", "false"):
                raise ValueError(f"{name} must be true or false")
            return value == "true"

        def api_config(prefix: str) -> APIConfig:
            return APIConfig(
                base_url=values.get(f"{prefix}_URL") or "https://api.openai.com/v1",
                model=values.get(f"{prefix}_MODEL") or "",
                api_key=values.get(f"{prefix}_API_KEY") or "",
                timeout=timeout,
                max_retries=max_retries,
                json_mode=values.get(f"{prefix}_JSON_MODE") or "json_object",
                max_concurrency=int(values.get(f"{prefix}_MAX_CONCURRENCY") or "4"),
                batch_size=int(values.get(f"{prefix}_BATCH_SIZE") or "64"),
                thinking_enabled=(
                    switch(f"{prefix}_THINKING_ENABLED")
                    if values.get(f"{prefix}_THINKING_ENABLED")
                    else None
                ),
                **generation_config(values, prefix),
            )

        return cls(
            db_path=Path(values.get("MEMORY_DM_DB_PATH") or "data/memory_dm.sqlite3"),
            user_id=values.get("MEMORY_DM_USER_ID") or "local",
            llm=api_config("LLM"),
            embedding=api_config("EMBEDDING"),
            output_language=(values.get("MEMORY_DM_OUTPUT_LANGUAGE") or "auto").strip() or "auto",
            forget_batch_size=int(values.get("FORGET_BATCH_SIZE") or "16"),
            forget_batch_chars=int(values.get("FORGET_BATCH_CHARS") or "24000"),
            forget_max_passes=int(values.get("FORGET_MAX_PASSES") or "2"),
            auto_memory=auto_memory == "true",
            memory_write_every=int(values.get("MEMORY_DM_WRITE_EVERY") or "5"),
            memory_context_turns=int(values.get("MEMORY_DM_CONTEXT_TURNS") or "12"),
            memory_top_k=int(values.get("MEMORY_DM_MEMORY_TOP_K") or "10"),
            memory_context_chars=int(values.get("MEMORY_DM_CONTEXT_CHARS") or "16000"),
            retrieval_context_tokens=int(values.get("RETRIEVAL_CONTEXT_TOKENS") or "16384"),
            retrieval_max_rounds=int(values.get("RETRIEVAL_MAX_ROUNDS") or "2"),
            retrieval_neighbor_turns=int(values.get("MEMORY_DM_NEIGHBOR_TURNS") or "2"),
            show_metrics=show_metrics == "true",
            experience_enabled=experience == "true",
            experience_batch_size=int(values.get("EXPERIENCE_BATCH_SIZE") or "16"),
            experience_retrieval_enabled=switch("EXPERIENCE_RETRIEVAL_ENABLED", True),
            profile_enabled=switch("PROFILE_ENABLED", True),
            memory_write_tokens=int(values.get("MEMORY_DM_WRITE_TOKENS") or "0"),
            forget_probe_count=int(values.get("FORGET_PROBE_COUNT") or "0"),
            forgetting=ForgettingOptions(
                strategy=(values.get("FORGET_STRATEGY") or "minimal").strip(),
                local_rewrite=switch("FORGET_LOCAL_REWRITE", True),
                source_revalidation=switch("FORGET_SOURCE_REVALIDATION", True),
                regenerate=switch("FORGET_REGENERATE", True),
            ),
            retrieval_methods=tuple(
                part.strip()
                for part in (values.get("RETRIEVAL_METHODS") or "hybrid,grep,time").split(",")
            ),
            router_model=(values.get("MEMORY_DM_ROUTER_MODEL") or "").strip() or None,
        )


def generation_config(values, prefix: str) -> dict:
    """Read shared generation settings for an LLM or an independently configured judge."""
    converters = {
        "provider": str,
        "reasoning_effort": str,
        "temperature": float,
        "top_p": float,
        "max_output_tokens": int,
        "seed": int,
        "presence_penalty": float,
        "frequency_penalty": float,
    }
    return {
        field: convert(values[f"{prefix}_{field.upper()}"])
        for field, convert in converters.items()
        if values.get(f"{prefix}_{field.upper()}")
    }


def judge_api_config(base: APIConfig, values, prefix: str, model: str | None = None) -> APIConfig:
    """Inherit backbone defaults, allowing every judge transport/generation setting to differ."""
    from dataclasses import replace

    overrides = generation_config(values, prefix)
    for attribute, suffix in (
        ("base_url", "URL"),
        ("api_key", "API_KEY"),
        ("model", "MODEL"),
        ("json_mode", "JSON_MODE"),
    ):
        if values.get(f"{prefix}_{suffix}"):
            overrides[attribute] = values[f"{prefix}_{suffix}"]
    raw_thinking = values.get(f"{prefix}_THINKING_ENABLED")
    if raw_thinking:
        if raw_thinking.strip().lower() not in ("true", "false"):
            raise ValueError(f"{prefix}_THINKING_ENABLED must be true or false")
        overrides["thinking_enabled"] = raw_thinking.strip().lower() == "true"
    if model:
        overrides["model"] = model
    return replace(base, **overrides)
