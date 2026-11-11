"""Validate LoCoMo's published JSON without importing another memory system."""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CATEGORIES = {1: "multi_hop", 2: "temporal", 3: "open_domain", 4: "single_hop", 5: "adversarial"}


@dataclass(frozen=True)
class DialogueTurn:
    speaker: str
    dia_id: str
    text: str
    caption: str = ""

    def content(self) -> str:
        # Both speakers are humans. Keep their identity in the text passed to memory.
        content = f"{self.speaker}: {self.text}"
        if self.caption:
            content += f"\n[Image caption shared by {self.speaker}: {self.caption}]"
        return content


@dataclass(frozen=True)
class DialogueSession:
    number: int
    timestamp: datetime
    turns: tuple[DialogueTurn, ...]


@dataclass(frozen=True)
class Question:
    index: int
    question: str
    category: int
    answer: str
    evidence: tuple[str, ...]
    distractor: str = ""


@dataclass(frozen=True)
class Sample:
    sample_id: str
    sessions: tuple[DialogueSession, ...]
    questions: tuple[Question, ...]


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"LoCoMo {field} must be nonblank text")
    return value


def load_dataset(path: Path, *, timezone: str = "UTC") -> list[Sample]:
    # The source provides wall-clock times without offsets; the assumption is explicit.
    zone = ZoneInfo(timezone)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("LoCoMo dataset must be a nonempty JSON array")
    samples, identifiers = [], set()
    for raw in data:
        sample_id = _text(raw.get("sample_id"), "sample_id")
        if sample_id in identifiers:
            raise ValueError(f"Duplicate LoCoMo sample_id: {sample_id}")
        identifiers.add(sample_id)
        conversation = raw["conversation"]
        speakers = {conversation["speaker_a"], conversation["speaker_b"]}
        sessions, turn_ids = [], set()
        keys = sorted(
            (k for k in conversation if re.fullmatch(r"session_[1-9][0-9]*", k)),
            key=lambda k: int(k.split("_")[1]),
        )
        for key in keys:
            timestamp = datetime.strptime(
                conversation[key + "_date_time"], "%I:%M %p on %d %B, %Y"
            ).replace(tzinfo=zone)
            if sessions and timestamp < sessions[-1].timestamp:
                raise ValueError(f"LoCoMo sessions are not chronological: {sample_id}")
            turns = []
            for turn in conversation[key]:
                speaker = _text(turn["speaker"], "speaker")
                dia_id = _text(turn["dia_id"], "dia_id")
                if speaker not in speakers or dia_id in turn_ids:
                    raise ValueError(f"Unknown speaker or duplicate dialogue ID: {sample_id}")
                turn_ids.add(dia_id)
                text, caption = turn.get("text", ""), turn.get("blip_caption", "")
                if not isinstance(text, str) or not isinstance(caption, str):
                    raise ValueError("LoCoMo turn text and image captions must be strings")
                if not (text.strip() or caption.strip()):
                    raise ValueError("LoCoMo turns require text or an image caption")
                turns.append(DialogueTurn(speaker, dia_id, text, caption))
            if not turns:
                raise ValueError("LoCoMo sessions must not be empty")
            sessions.append(DialogueSession(int(key.split("_")[1]), timestamp, tuple(turns)))
        if not sessions:
            raise ValueError(f"LoCoMo sample has no sessions: {sample_id}")
        questions = []
        for index, qa in enumerate(raw["qa"]):
            category = qa["category"]
            if type(category) is not int or category not in CATEGORIES:
                raise ValueError("LoCoMo category must be an integer from 1 to 5")
            evidence = qa.get("evidence", [])
            if not isinstance(evidence, list) or any(not isinstance(e, str) for e in evidence):
                raise ValueError("LoCoMo evidence must be a list of dialogue IDs")
            # Some released annotations reference malformed/missing evidence IDs.
            # Evidence is retained for inspection, never used to select memory inputs.
            answer = qa.get("answer")
            distractor = qa.get("adversarial_answer", answer) if category == 5 else ""
            reference = distractor if category == 5 else answer
            if isinstance(reference, bool) or not isinstance(reference, (str, int, float)):
                raise ValueError("LoCoMo questions require an answer or adversarial distractor")
            _text(str(reference), "answer")
            questions.append(
                Question(
                    index,
                    _text(qa["question"], "question"),
                    category,
                    "Not mentioned in the conversation" if category == 5 else str(answer),
                    tuple(evidence),
                    str(distractor),
                )
            )
        if not questions:
            raise ValueError(f"LoCoMo sample has no questions: {sample_id}")
        samples.append(Sample(sample_id, tuple(sessions), tuple(questions)))
    return samples
