"""Control-pair evaluation runner.

Protocol per dialogue instance:
1. `adapter.reset()`; observe `control_sessions` turn by turn; run Retention
   plus the control-variant calibration probes (preservation and attack on
   intact memory).
2. `adapter.reset()`; observe `sessions` (forget variant); run Efficacy,
   Preservation, Attack, Relearning — in that order. Attack is probed before
   Relearning so the adversarial-extraction suite runs on the post-unlearning
   state, before any relearning short dialogue re-introduces same-type facts.

Probe answers are never written back to memory (adapter contract), so probes
cannot contaminate each other.

With separate adapters, the control and forget variants can run concurrently.
Each variant retains its own ingestion and probe ordering, and records are
returned in control-then-forget order regardless of completion order.

Both Preservation and Attack are measured on the forget variant *and* on the
intact control variant: a system that never stored an unrelated fact cannot be
credited with preserving it, just as an attack that cannot extract the target
from intact memory proves nothing about robustness.
"""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
from typing import Any

from pydantic import BaseModel
from tqdm import tqdm

from memory_dm.evaluation._vendor.bemula.baselines.base import MemoryAdapter
from memory_dm.evaluation._vendor.bemula.eval.judge import Judge
from memory_dm.evaluation._vendor.bemula.schemas import (
    AttackType,
    DialogueInstance,
    ForgetType,
    Grading,
    Session,
    Suite,
    TestItem,
)

# Suites run on the forget variant, in protocol order. Attack precedes
# Relearning: the robustness suite must measure the memory right after
# forgetting, not after relearning has re-seeded same-type values.
FORGET_SUITE_ORDER = (
    Suite.EFFICACY,
    Suite.PRESERVATION,
    Suite.ATTACK,
    Suite.RELEARNING,
)


class ItemRecord(BaseModel):
    """One probed item with the system's answer and its verdict."""

    item_id: str
    instance_id: str
    suite: Suite
    attack_type: AttackType | None = None
    # Forget type of the item's request (None for instance-level preservation
    # items, which are not tied to a single request).
    forget_type: ForgetType | None = None
    request_id: str | None = None
    grading: Grading
    question: str
    gold_answer: str
    answer: str
    correct: bool
    leakage: bool | None = None  # attack items only
    # Which dialogue variant this probe ran on. Retention always runs on the
    # control variant; attack items run on BOTH (the control run calibrates
    # whether the attack can extract the target when memory is intact).
    variant: str = "forget"  # "control" | "forget"
    # Pairing metadata for the per-item efficacy condition:
    # an efficacy item shares (request_id, probe_type, fact) with a retention
    # item run on the control variant.
    probe_type: str = "direct"
    related_fact_ids: list[str] = []
    # Instance length bucket (short | medium | long | xlong), copied from the
    # instance's difficulty so records can be stratified without a data join.
    # None only for records written before this field existed.
    length_bucket: str | None = None


def _observe(adapter: MemoryAdapter, sessions: Iterable[Session]) -> None:
    start = getattr(adapter, "start_observation_scope", lambda _: 0)("dialogue")
    index = 0
    for session in sessions:
        adapter.begin_session(session)
        for turn in session.turns:
            if index >= start:
                adapter.observe(turn)
            index += 1
    adapter.end_variant()
    complete = getattr(adapter, "complete_dialogue_checkpoint", None)
    if complete is not None:
        complete()


def _probe(
    adapter: MemoryAdapter,
    judge: Judge,
    instance: DialogueInstance,
    item: TestItem,
    variant: str = "forget",
) -> ItemRecord:
    # Relearning items carry a short dialogue stating the new information;
    # it is observed right before the question so the system must have learned
    # it post-unlearning to answer.
    start = getattr(adapter, "start_observation_scope", lambda _: 0)(f"context:{item.item_id}")
    for index, turn in enumerate(item.context_turns):
        if index >= start:
            adapter.observe(turn)
    answer = adapter.query(item.question)
    verdict = judge.judge(item, answer)
    # Stratification is request-level: look up the item's forget type via its
    # request_id; instance-level items fall back to None.
    request_types = {r.request_id: r.forget_type for r in instance.forget_requests}
    forget_type = request_types.get(item.request_id) if item.request_id else None
    return ItemRecord(
        item_id=item.item_id,
        instance_id=instance.instance_id,
        suite=item.suite,
        attack_type=item.attack_type,
        forget_type=forget_type,
        request_id=item.request_id,
        grading=item.grading,
        question=item.question,
        gold_answer=item.gold_answer,
        answer=answer,
        correct=verdict.correct,
        leakage=verdict.leakage,
        variant=variant,
        probe_type=item.probe_type,
        related_fact_ids=item.related_fact_ids,
        length_bucket=instance.difficulty.length_bucket,
    )


def run_instance(
    adapter: MemoryAdapter,
    instance: DialogueInstance,
    items: list[TestItem],
    judge: Judge,
    *,
    control_adapter: MemoryAdapter | None = None,
    resume_records: list[ItemRecord] | None = None,
    on_item: Any = None,
) -> list[ItemRecord]:
    """Run the pair, optionally concurrently with an isolated control adapter."""
    if control_adapter is adapter:
        raise ValueError("Concurrent variants require separate adapters")
    by_suite: dict[Suite, list[TestItem]] = {}
    for item in items:
        by_suite.setdefault(item.suite, []).append(item)
    saved = {(r.variant, r.item_id): r for r in (resume_records or [])}

    def run_variant(memory: MemoryAdapter, variant: str) -> list[ItemRecord]:
        memory.begin_variant(variant)
        memory.reset()
        sessions = instance.control_sessions if variant == "control" else instance.sessions
        _observe(memory, sessions)
        suites = (Suite.RETENTION, Suite.PRESERVATION, Suite.ATTACK) if variant == "control" \
            else FORGET_SUITE_ORDER
        records = []
        for suite in suites:
            for item in by_suite.get(suite, []):
                key = (variant, item.item_id)
                if key in saved:
                    records.append(saved[key])
                    continue
                record = _probe(memory, judge, instance, item, variant=variant)
                records.append(record)
                if on_item is not None:
                    on_item(record)
        return records

    adapter.begin_instance(instance)
    try:
        if control_adapter is None:
            return run_variant(adapter, "control") + run_variant(adapter, "forget")
        control_adapter.begin_instance(instance)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                control = pool.submit(run_variant, control_adapter, "control")
                forget = pool.submit(run_variant, adapter, "forget")
                control_records = control.result()
                forget_records = forget.result()
                return control_records + forget_records
        finally:
            control_adapter.end_instance()
    finally:
        adapter.end_instance()


def run_evaluation(
    adapter: MemoryAdapter,
    instances: list[DialogueInstance],
    items_by_instance: dict[str, list[TestItem]],
    judge: Judge,
) -> list[ItemRecord]:
    """Run the protocol over all instances and collect item records."""
    records: list[ItemRecord] = []
    for instance in instances:
        records.extend(
            run_instance(adapter, instance, items_by_instance.get(instance.instance_id, []), judge)
        )
    return records


def run_evaluation_parallel(
    adapters: list[MemoryAdapter],
    instances: list[DialogueInstance],
    items_by_instance: dict[str, list[TestItem]],
    judge: Judge,
    on_instance: Any = None,
    *,
    control_adapters: list[MemoryAdapter] | None = None,
    resume_records_by_instance: dict[str, list[ItemRecord]] | None = None,
    on_item: Any = None,
) -> list[ItemRecord]:
    """Instance-level parallel evaluation with one dedicated adapter per worker.

    Instances are independent (each run_instance starts with reset()), so
    workers draw adapters from a pool, run a full instance, and return them.
    Supplying control_adapters runs each pair of variants concurrently, with
    one dedicated adapter for each variant and at most twice as many active
    variant workflows as instance workers.
    Adapters are constructed by the caller (sequentially) so heavyweight
    init (local vector stores, migrations) does not race. `on_instance` is
    invoked with each finished instance's records (for checkpointing).
    """
    if control_adapters is not None and len(control_adapters) != len(adapters):
        raise ValueError("Each worker needs one control adapter")
    all_adapters = adapters + (control_adapters or [])
    if len({id(a) for a in all_adapters}) != len(all_adapters):
        raise ValueError("Workers and concurrent variants require separate adapters")
    pool_queue: Queue[tuple[MemoryAdapter, MemoryAdapter | None]] = Queue()
    for index, adapter in enumerate(adapters):
        pool_queue.put((adapter, control_adapters[index] if control_adapters is not None else None))

    def work(instance: DialogueInstance) -> list[ItemRecord]:
        adapter, control_adapter = pool_queue.get()
        try:
            return run_instance(
                adapter, instance, items_by_instance.get(instance.instance_id, []), judge,
                control_adapter=control_adapter,
                resume_records=(resume_records_by_instance or {}).get(instance.instance_id, []),
                on_item=on_item,
            )
        finally:
            pool_queue.put((adapter, control_adapter))

    records: list[ItemRecord] = []
    first_error: Exception | None = None
    with ThreadPoolExecutor(max_workers=len(adapters)) as pool:
        futures = [pool.submit(work, inst) for inst in instances]
        with tqdm(total=len(instances), desc="eval", unit="inst") as bar:
            for fut in as_completed(futures):
                try:
                    recs = fut.result()
                except Exception as exc:
                    if first_error is None:
                        first_error = exc
                    bar.update(1)
                    continue
                records.extend(recs)
                if on_instance is not None:
                    on_instance(recs)
                bar.update(1)
    if first_error is not None:
        raise first_error
    return records
