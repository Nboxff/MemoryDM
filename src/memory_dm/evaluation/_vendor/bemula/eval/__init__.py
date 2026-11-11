"""Evaluation framework: judging, control-pair runner, metrics."""

from memory_dm.evaluation._vendor.bemula.eval.judge import Judge, LLMJudge, MockJudge, Verdict
from memory_dm.evaluation._vendor.bemula.eval.metrics import compute_metrics
from memory_dm.evaluation._vendor.bemula.eval.runner import ItemRecord, run_evaluation, run_instance

__all__ = [
    "Judge",
    "LLMJudge",
    "MockJudge",
    "Verdict",
    "compute_metrics",
    "ItemRecord",
    "run_evaluation",
    "run_instance",
]
