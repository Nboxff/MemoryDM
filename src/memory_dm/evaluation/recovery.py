"""Restore completed ingestion states without replaying expensive memory writes."""

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from memory_dm.evaluation.adapter import MemoryDMAdapter
from memory_dm.memory.agent import MemoryAgent
from memory_dm.storage.sqlite import SQLiteStore


def check_compatible(previous: dict, current: dict) -> None:
    """Recovery may upgrade the harness, but never the evaluated memory system or dataset."""
    for key in current:
        if key == "memory_dm_source":
            old_core = {k: v for k, v in previous[key].items() if not k.startswith("evaluation/")}
            new_core = {k: v for k, v in current[key].items() if not k.startswith("evaluation/")}
            if old_core != new_core:
                raise ValueError("Recovery requires unchanged memory code and prompts")
        elif key == "judge_max_tokens":
            continue
        elif previous.get(key) != current[key]:
            raise ValueError(f"Recovery configuration differs: {key}")


def prepare_snapshots(source: Path, destination: Path) -> dict[str, Path]:
    """Copy immutable snapshots, or safe legacy states whose probes never wrote anything."""
    result = {}
    for directory in sorted((source / "states").iterdir()):
        events_path = directory / "events.jsonl"
        if not events_path.is_file():
            continue
        events = [json.loads(line) for line in events_path.read_text().splitlines()]
        if not events:
            continue
        target = destination / directory.name
        for variant in ("control", "forget"):
            group = [event for event in events if event["variant"] == variant]
            boundaries = [i for i, event in enumerate(group) if event["operation"] == "end_variant"]
            if not boundaries:
                continue
            boundary = boundaries[-1]
            snapshot = directory / (variant + ".snapshot.sqlite3")
            if not snapshot.is_file():
                # Old runs did not snapshot. Their current DB is usable only when
                # nothing except read-only probes happened after completed ingestion.
                later = group[boundary + 1 :]
                if any(event["operation"] not in ("query", "end_instance") for event in later):
                    continue
                snapshot = directory / (variant + ".sqlite3")
            target.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect("file:" + str(snapshot) + "?mode=ro", uri=True) as original:
                with sqlite3.connect(target / (variant + ".sqlite3")) as copy:
                    original.backup(copy)
            (target / (variant + ".events.json")).write_text(
                json.dumps(group[: boundary + 1]) + "\n", encoding="utf-8"
            )
            result[events[0]["instance_id"]] = target
    return result


class RestoredAdapter(MemoryDMAdapter):
    def __init__(self, *args, restore_from: Path, **kwargs):
        super().__init__(*args, **kwargs)
        self.restore_from = restore_from
        self.restoring = False

    def reset(self) -> None:
        super().reset()
        snapshot = self.restore_from / (self.variant + ".sqlite3")
        self.restoring = snapshot.is_file()
        if not self.restoring:
            return
        self.close()
        shutil.copyfile(snapshot, self.directory / (self.variant + ".sqlite3"))
        self.store = SQLiteStore(self.directory / (self.variant + ".sqlite3"))
        self.agent = MemoryAgent(self.store, self.settings, self.llm, self.embedding)
        shutil.copyfile(snapshot, self.directory / (self.variant + ".snapshot.sqlite3"))
        self.events.extend(
            json.loads((self.restore_from / (self.variant + ".events.json")).read_text())
        )
        self._event("restore", source=str(snapshot))
        if self.progress:
            print(f"{self.instance_id} [{self.variant}] restored completed ingestion", flush=True)

    def begin_session(self, session) -> None:
        if not self.restoring:
            super().begin_session(session)

    def observe(self, turn) -> None:
        if not self.restoring:
            super().observe(turn)
            return
        timestamp = datetime.fromisoformat(turn.timestamp.replace("Z", "+00:00"))
        self.last_timestamp = max(self.last_timestamp or timestamp, timestamp)

    def end_variant(self) -> None:
        if not self.restoring:
            super().end_variant()
            return
        self.restoring = False
        self.session = None
        self.relearning = True
        self._event("end_variant")
