"""Replay benchmark dialogue through native memory operations, without gold metadata."""

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from memory_dm.config import Settings
from memory_dm.errors import ReconciliationError, RetrievalError
from memory_dm.forgetting.models import ForgettingError
from memory_dm.llm.clients import ClientError, EmbeddingClient, LLMClient
from memory_dm.memory.agent import MemoryAgent
from memory_dm.memory.state import derived_failures
from memory_dm.models import Turn
from memory_dm.storage.sqlite import SQLiteStore

SYSTEM_ERRORS = (ReconciliationError, RetrievalError, ForgettingError, ValueError)
FAILED_PROBE = "MemoryDM could not answer this probe because an internal memory operation failed."


class MemoryDMAdapter:
    """Implement Bemula's lifecycle without depending on its optional package at import time."""

    name = "memory_dm_native"

    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        embedding: EmbeddingClient | None,
        directory: Path,
        *,
        progress: bool = True,
    ):
        self.settings, self.llm, self.embedding = settings, llm, embedding
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=False)
        self.progress = progress
        self.store = None
        self.agent = None
        self.session = None
        self.instance_id = None
        self.variant = None
        self.variants = 0
        self.session_count = 0
        self.last_timestamp = None
        self.relearning = False
        self.events: list[dict] = []

    def _event(self, operation: str, **details) -> None:
        event = {
            "instance_id": self.instance_id,
            "variant": self.variant,
            "operation": operation,
            **details,
        }
        self.events.append(event)
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(event, ensure_ascii=False) + "\n")

    def begin_instance(self, instance: Any) -> None:
        # Only the instance identifier is used for bookkeeping, never its gold targets.
        self.instance_id = instance.instance_id

    def begin_variant(self, variant: str) -> None:
        """Check the paired evaluator's branch before opening its database."""
        if self.variants >= 2 or variant != ("control", "forget")[self.variants]:
            raise ValueError("Expected the control branch followed by the forget branch")

    def reset(self) -> None:
        self.close()
        if self.variants >= 2:
            raise ValueError("Use one adapter per paired dialogue instance")
        self.variant = ("control", "forget")[self.variants]
        self.variants += 1
        self.store = SQLiteStore(self.directory / (self.variant + ".sqlite3"))
        self.agent = MemoryAgent(
            self.store,
            self.settings,
            self.llm,
            self.embedding,
            before_forget=self._snapshot_before_forget,
        )
        self.session = None
        self.session_count = 0
        self.last_timestamp = None
        self.relearning = False
        self._event("reset")

    def _snapshot_before_forget(self):
        from memory_dm.evaluation.snapshots import snapshot

        directory = self.directory / "before-forget"
        number = len(list(directory.glob("*.sqlite3"))) if directory.exists() else 0
        path = directory / f"{number:04d}.sqlite3"
        snapshot(self.store.connection, path)
        self._event(
            "before_forget_snapshot",
            path=str(path.relative_to(self.directory)),
            reference_time=self.last_timestamp.isoformat() if self.last_timestamp else None,
        )

    def _flush(self) -> None:
        if self.session is None:
            return
        try:
            report = self.agent.end_session(self.session)
        except SYSTEM_ERRORS as error:
            self._event(
                "system_error",
                stage="flush",
                error_type=type(error).__name__,
                message=str(error),
                diagnostics=getattr(error, "diagnostics", {}),
            )
            return
        self._event("flush", **asdict(report))
        for failure in derived_failures(report):
            self._event("system_error", **failure)
        for failure in report.recovery_failures:
            self._event("system_error", stage="maintenance_recovery", **failure)

    def begin_session(self, session: Any) -> None:
        self._flush()
        self.session = self.store.create_session("benchmark-user")
        self.session_count += 1
        # Session labels, themes and linked fact IDs are not user-visible memory input.
        self._event("begin_session", session_number=self.session_count)
        if self.progress:
            print(f"{self.instance_id} [{self.variant}] session {self.session_count}", flush=True)

    def observe(self, turn: Any) -> None:
        timestamp = (
            datetime.fromisoformat(turn.timestamp.replace("Z", "+00:00"))
            if turn.timestamp
            else self.last_timestamp + timedelta(seconds=1)
            if self.last_timestamp is not None
            else None
        )
        if timestamp is None or timestamp.tzinfo is None:
            raise ValueError("Dialogue turns require timezone-aware source timestamps")
        self.last_timestamp = max(self.last_timestamp or timestamp, timestamp)
        if self.session is None:
            self.session = self.store.create_session("benchmark-user")
        native_turn = Turn(
            session_id=self.session.session_id,
            role=turn.speaker,
            content=turn.text,
            timestamp=timestamp,
        )
        try:
            result = self.agent.observe(self.session, native_turn)
        except SYSTEM_ERRORS as error:
            self._event(
                "system_error", stage="observe", error_type=type(error).__name__, message=str(error)
            )
            return
        details = {"speaker": turn.speaker, "timestamp": timestamp.isoformat()}
        if result.routing is not None:
            details["routing"] = result.routing
        if result.write is not None:
            details["write"] = asdict(result.write)
        if result.forget is not None:
            details["forget"] = result.forget.model_dump(mode="json")
        if result.maintenance_error:
            details["maintenance_error"] = result.maintenance_error
            details["maintenance_diagnostics"] = result.maintenance_diagnostics
        self._event("observe", **details)
        if result.forget is not None:
            for failure in result.forget.operation_failures:
                self._event(
                    "system_error",
                    stage="forget_" + failure["stage"],
                    error_type=failure["error_code"],
                    layer=failure["layer"],
                    record_id=failure["id"],
                )
        if result.write is not None:
            for failure in derived_failures(result.write):
                self._event("system_error", **failure)
            for failure in result.write.recovery_failures:
                self._event("system_error", stage="maintenance_recovery", **failure)

    def end_variant(self) -> None:
        self._flush()
        self.session = None
        self.relearning = True
        self._event("end_variant")
        # Preserve the state before any probe or relearning dialogue can affect it.
        with sqlite3.connect(self.directory / (self.variant + ".snapshot.sqlite3")) as target:
            self.store.connection.backup(target)

    def query(self, question: str) -> str:
        # Relearning dialogues have no session lifecycle callback in Bemula's runner.
        # Flush their final partial batch before the fresh-session probe.
        if self.relearning and self.session is not None:
            self._flush()
            self.session = None
        try:
            reply = self.agent.query(
                "benchmark-user",
                question,
                reference_time=self.last_timestamp or datetime.now(UTC),
            )
        except SYSTEM_ERRORS as error:
            self._event(
                "system_error", stage="query", error_type=type(error).__name__, message=str(error)
            )
            return FAILED_PROBE
        self._event("query", metrics=reply.metrics)
        return reply.content

    def end_instance(self) -> None:
        self._event("end_instance")
        self.close()

    def close(self) -> None:
        if self.store is not None:
            self.store.close()
            self.store = None
            self.agent = None


class JudgeLLM:
    """Use MemoryDM credentials with the official Bemula LLMJudge prompt and temperature."""

    def __init__(self, client: LLMClient):
        self.client = client
        self.last_error = None

    def chat(self, messages, *, temperature=0.0, max_tokens=4096, task="judge") -> str:
        try:
            return self._chat(messages, temperature, max_tokens)
        except ClientError as error:
            self.last_error = f"{type(error).__name__}: {error}"
            raise

    def _chat(self, messages, temperature, max_tokens):
        return self.client.chat(
            [
                Turn(session_id="benchmark-judge", role=message["role"], content=message["content"])
                for message in messages
            ],
            temperature=temperature,
            max_tokens=max(16384, max_tokens),
            response_format={"type": "json_object"},
        )
