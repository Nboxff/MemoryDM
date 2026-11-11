"""Optional post-commit answer probes; no forgotten text is stored in reports."""

import sqlite3
from typing import Literal

from pydantic import Field

from memory_dm.errors import RetrievalError
from memory_dm.forgetting.models import ForgettingError
from memory_dm.llm.clients import ClientError
from memory_dm.llm.structured import ResponseValidationError, StructuredResponseError
from memory_dm.models import Record


class AnswerProbe(Record):
    kind: Literal["TARGET", "PRESERVATION"]
    question: str = Field(max_length=500)
    criterion: str = Field(max_length=1000)


class AnswerProbes(Record):
    probes: list[AnswerProbe] = Field(min_length=1, max_length=5)


class ProbeVerdict(Record):
    passed: bool = Field(strict=True)


def run_probes(semantic, spec, before, count: int, answer) -> list[dict]:
    if not count:
        return []
    try:
        records = [{"layer": "ATOMIC", "content": m.content} for m in before.memories.values()]
        records += [{"layer": "RAW", "content": t.content} for t in before.turns.values()]
        selected, size = [], 0
        for record in records:
            if size + len(record["content"]) > 16000:
                continue
            selected.append(record)
            size += len(record["content"])
            if len(selected) == 36:
                break

        def validate(proposed):
            if len(proposed.probes) > count or not any(p.kind == "TARGET" for p in proposed.probes):
                raise ResponseValidationError(
                    "Respect probe_count and include a target leakage probe"
                )

        proposed = semantic._call(
            "forget_probes.md",
            {
                "spec": spec.model_dump(mode="json"),
                "original_records": selected,
                "probe_count": count,
            },
            AnswerProbes,
            "forget_probes",
            validate=validate,
            temperature=0,
        )
    except (
        ClientError,
        StructuredResponseError,
        RetrievalError,
        ValueError,
        ForgettingError,
        sqlite3.Error,
    ):
        return [{"kind": "TARGET", "passed": False, "error_code": "PROBE_GENERATION_FAILED"}]
    results = []
    for probe in proposed.probes:
        try:
            reply = answer(probe.question)
            verdict = semantic._call(
                "forget_probe_judge.md",
                {"probe": probe.model_dump(mode="json"), "answer": reply},
                ProbeVerdict,
                "forget_probe_judge",
                temperature=0,
            )
            results.append({"kind": probe.kind, "passed": verdict.passed})
        except (
            ClientError,
            StructuredResponseError,
            RetrievalError,
            ValueError,
            ForgettingError,
            sqlite3.Error,
        ):
            results.append(
                {"kind": probe.kind, "passed": False, "error_code": "PROBE_CHECK_FAILED"}
            )
    return results
