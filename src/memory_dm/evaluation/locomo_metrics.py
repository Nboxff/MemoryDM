"""Lightweight compatibility with snap-research/locomo's QA scoring rules.

Reference: task_eval/evaluation.py in https://github.com/snap-research/locomo.
This implementation avoids BERTScore/model downloads and preserves the original
Porter stemming, multiset F1, comma-separated multi-hop, and category-3 rules.
"""

import re
import string
from collections import Counter
from functools import lru_cache
from importlib.metadata import version
from statistics import mean

from memory_dm.evaluation.locomo_data import CATEGORIES, Question


@lru_cache(maxsize=1)
def _stemmer():
    try:
        from nltk.stem import PorterStemmer
    except ImportError as error:
        raise RuntimeError("Install LoCoMo scoring dependencies: uv sync --extra eval") from error
    # PorterStemmer requires no downloaded corpora or tokenizer resources.
    return PorterStemmer()


def scorer_metadata() -> dict:
    _stemmer()
    return {"rules": "snap_research_locomo_qa_v1", "nltk": version("nltk")}


def normalize_answer(text: str) -> str:
    text = text.lower().translate(str.maketrans("", "", string.punctuation))
    return " ".join(re.sub(r"\b(a|an|the|and)\b", " ", text).split())


def token_f1(prediction: str, reference: str) -> float:
    stem = _stemmer().stem
    predicted = Counter(stem(t) for t in normalize_answer(prediction).split())
    expected = Counter(stem(t) for t in normalize_answer(reference).split())
    overlap = sum((predicted & expected).values())
    total = predicted.total() + expected.total()
    return 2 * overlap / total if overlap else 0.0


def score_prediction(prediction: str, question: Question) -> float:
    if question.category == 5:
        # These literal markers match the published scorer, not a semantic judge.
        return float(
            "no information available" in prediction.lower()
            or "not mentioned" in prediction.lower()
        )
    reference = question.answer
    if question.category == 3:
        reference = reference.split(";")[0].strip()
    if question.category == 1:
        parts = [part.strip() for part in prediction.split(",")]
        return mean(
            max(token_f1(part, expected.strip()) for part in parts)
            for expected in reference.split(",")
        )
    return token_f1(prediction, reference)


def bleu1(prediction: str, reference: str) -> float:
    """Sentence BLEU-1: clipped unigram precision with brevity penalty, no stemming."""
    import math

    predicted = prediction.lower().split()
    expected = reference.lower().split()
    if not predicted or not expected:
        return 0.0
    overlap = sum((Counter(predicted) & Counter(expected)).values())
    return overlap / len(predicted) * math.exp(min(0, 1 - len(expected) / len(predicted)))


def score_bleu1(prediction: str, question: Question) -> float | None:
    if question.category == 5:
        return None
    reference = question.answer.split(";")[0].strip() if question.category == 3 else question.answer
    return bleu1(prediction, reference)


def aggregate_retrieval_cost(records: list[dict], expected: int) -> dict:
    """Summarize saved retrieval traces, independently of invocation/API totals.

    Sum traces within each question before averaging. Missing traces are unknown,
    never zero-cost questions. Tokens exclude embedding and final-answer calls.
    """
    measured = []
    for record in records:
        traces = (record.get("metrics") or {}).get("retrieval")
        if not traces:
            continue
        measured.append(
            {
                "llm_calls": sum(t["llm"]["calls"] for t in traces),
                "llm_input_tokens": sum(t["llm"]["input_tokens"] for t in traces),
                "llm_output_tokens": sum(t["llm"]["output_tokens"] for t in traces),
                "llm_usage_missing": sum(t["llm"]["usage_missing"] for t in traces),
                "latency_seconds": sum(t["latency_ms"] for t in traces) / 1000,
            }
        )
    totals = {
        key: sum(item[key] for item in measured)
        for key in (
            "llm_calls", "llm_input_tokens", "llm_output_tokens",
            "llm_usage_missing", "latency_seconds",
        )
    }
    totals["llm_tokens"] = totals["llm_input_tokens"] + totals["llm_output_tokens"]
    count = len(measured)
    return {
        "scope": "all_selected_categories_saved_traces",
        "expected_questions": expected,
        "measured_questions": count,
        "missing_questions": expected - count,
        "coverage_complete": count == expected,
        "token_usage_complete": bool(count) and totals["llm_usage_missing"] == 0,
        "totals": totals,
        "mean_llm_calls": totals["llm_calls"] / count if count else None,
        "mean_llm_tokens": (
            totals["llm_tokens"] / count
            if count and totals["llm_usage_missing"] == 0 else None
        ),
        "mean_latency_seconds": totals["latency_seconds"] / count if count else None,
    }


def aggregate(records: list[dict], expected: int) -> dict:
    categories = {}
    for number, name in CATEGORIES.items():
        values = [r["score"] for r in records if r["category"] == number]
        categories[str(number)] = {
            "name": name,
            "count": len(values),
            "mean_score": mean(values) if values else None,
            "metric": "accuracy" if number == 5 else "f1",
        }
    factual = [r["score"] for r in records if r["category"] != 5]
    bleu = [r["bleu1"] for r in records if r.get("bleu1") is not None]
    return {
        "retrieval_cost": aggregate_retrieval_cost(records, expected),
        "bleu1": mean(bleu) if bleu else None,
        "expected_questions": expected,
        "scored_questions": len(records),
        "successful_questions": sum(r["status"] == "completed" for r in records),
        "failed_questions": sum(r["status"] == "error" for r in records),
        "f1": mean(factual) if factual else None,
        "adversarial_accuracy": categories["5"]["mean_score"],
        "overall_official_score": mean(r["score"] for r in records) if records else None,
        "by_category": categories,
    }
