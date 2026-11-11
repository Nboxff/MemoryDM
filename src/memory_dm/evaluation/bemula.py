"""Run Bemula's official control-pair protocol against MemoryDM's native pipeline."""

import argparse
import importlib
import json
import os
import shutil
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from dotenv import dotenv_values

from memory_dm.config import Settings, judge_api_config
from memory_dm.evaluation.adapter import FAILED_PROBE, JudgeLLM, MemoryDMAdapter
from memory_dm.evaluation.common import digest as _digest
from memory_dm.evaluation.common import git as _git
from memory_dm.evaluation.common import settings_metadata as _settings_metadata
from memory_dm.evaluation.common import write_json as _write_json
from memory_dm.evaluation.recovery import RestoredAdapter, check_compatible, prepare_snapshots
from memory_dm.evaluation.variants import add_variant_arguments, apply_variant
from memory_dm.llm.clients import EmbeddingClient, LLMClient


def load_benchmark(root: Path | None = None):
    """Load the bundled evaluator or an explicitly supplied Bemula checkout."""
    prefix = "memory_dm.evaluation._vendor.bemula"
    if root is not None:
        root = root.resolve()
        if not (root / "bemula/eval/runner.py").is_file():
            raise ValueError("--bemula-root must contain the Bemula source checkout")
        imported = sys.modules.get("bemula")
        if imported is not None and Path(imported.__file__).resolve().parent != root / "bemula":
            raise ValueError("A different Bemula checkout is already imported")
        sys.path.insert(0, str(root))
        prefix = "bemula"
    try:
        return SimpleNamespace(
            **{
                name: importlib.import_module(prefix + "." + module)
                for name, module in (
                    ("schemas", "schemas"),
                    ("runner", "eval.runner"),
                    ("judge", "eval.judge"),
                    ("metrics", "eval.metrics"),
                )
            }
        )
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "Install optional evaluation dependencies: uv sync --extra eval"
        ) from error


class CheckedJudge:
    """Keep system failures incorrect, and abort on judge failures instead of inventing scores."""

    def __init__(self, benchmark, client):
        self.benchmark = benchmark
        self.inner = benchmark.judge.LLMJudge(JudgeLLM(client), temperature=0.0)

    def judge(self, item, answer):
        if answer == FAILED_PROBE:
            return self.benchmark.judge.Verdict(
                correct=False, leakage=False if item.suite.value == "attack" else None
            )
        verdict = self.inner.judge(item, answer)
        if self.inner.failures:
            detail = self.inner.llm.last_error or "invalid judge JSON"
            raise RuntimeError(
                f"Bemula judge failed ({detail}); this instance has no valid checkpoint"
            )
        return verdict


def expected_probes(items) -> set[tuple[str, str]]:
    keys = set()
    for item in items:
        if item.suite.value in ("retention", "preservation", "attack"):
            keys.add((item.item_id, "control"))
        if item.suite.value != "retention":
            keys.add((item.item_id, "forget"))
    return keys


def _load_data(benchmark, dialogues: Path, questions: Path, limit: int | None):
    instances = benchmark.schemas.load_jsonl(dialogues, benchmark.schemas.DialogueInstance)
    items = benchmark.schemas.load_jsonl(questions, benchmark.schemas.TestItem)
    if len({instance.instance_id for instance in instances}) != len(instances):
        raise ValueError("Duplicate dialogue instance IDs")
    if len({item.item_id for item in items}) != len(items):
        raise ValueError("Duplicate question item IDs")
    known = {instance.instance_id: instance for instance in instances}
    by_instance = {instance_id: [] for instance_id in known}
    for item in items:
        if item.instance_id not in known:
            raise ValueError("A question refers to an unknown instance")
        request_ids = {request.request_id for request in known[item.instance_id].forget_requests}
        if item.request_id is not None and item.request_id not in request_ids:
            raise ValueError("A question refers to an unknown forget request")
        by_instance[item.instance_id].append(item)
    instances = instances[:limit] if limit is not None else instances
    if not instances or any(not by_instance[instance.instance_id] for instance in instances):
        raise ValueError("Every selected dialogue instance must have evaluation questions")
    return instances, by_instance


def _run_branch(
    benchmark, settings, judge_config, instance, items, directory, variant, restore_from=None
):
    """One branch owns its clients, database, judge and ordered lifecycle."""
    import faiss

    faiss.omp_set_num_threads(1)
    with ExitStack() as stack:
        llm = stack.enter_context(LLMClient(settings.llm))
        embedding = stack.enter_context(EmbeddingClient(settings.embedding))
        judge_client = stack.enter_context(LLMClient(judge_config))
        adapter_class = MemoryDMAdapter if restore_from is None else RestoredAdapter
        kwargs = {} if restore_from is None else {"restore_from": restore_from}
        adapter = adapter_class(settings, llm, embedding, directory, **kwargs)
        stack.callback(adapter.close)
        adapter.variants = 0 if variant == "control" else 1
        judge = CheckedJudge(benchmark, judge_client)
        records = []
        try:
            adapter.begin_instance(instance)
            adapter.reset()
            sessions = instance.control_sessions if variant == "control" else instance.sessions
            # Keep the supplied official observation/probe helpers and suite ordering.
            benchmark.runner._observe(adapter, sessions)
            suites = (
                (
                    benchmark.schemas.Suite.RETENTION,
                    benchmark.schemas.Suite.PRESERVATION,
                    benchmark.schemas.Suite.ATTACK,
                )
                if variant == "control"
                else benchmark.runner.FORGET_SUITE_ORDER
            )
            for suite in suites:
                for item in items:
                    if item.suite == suite:
                        records.append(
                            benchmark.runner._probe(adapter, judge, instance, item, variant=variant)
                        )
            adapter.end_instance()
        finally:
            usage = {
                "llm": asdict(llm.usage()),
                "embedding": asdict(embedding.usage()),
                "judge": asdict(judge_client.usage()),
            }
            _write_json(directory / "usage.json", usage)
    return records, adapter.events, usage


def _run_parallel_pair(
    benchmark, settings, judge_config, instance, items, directory, restore_from=None
):
    started = time.perf_counter()
    directory.mkdir(parents=True, exist_ok=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _run_branch,
                benchmark,
                settings,
                judge_config,
                instance,
                items,
                directory / variant,
                variant,
                restore_from,
            )
            for variant in ("control", "forget")
        ]
        # Canonical merge order is independent of completion timing.
        results = [future.result() for future in futures]
    records = [record for branch, _, _ in results for record in branch]
    events = [event for _, branch, _ in results for event in branch]
    usage = {
        kind: {field: sum(u[kind][field] for _, _, u in results) for field in results[0][2][kind]}
        for kind in results[0][2]
    }
    _write_json(directory / "usage.json", usage)
    with (directory / "events.jsonl").open("w") as output:
        for event in events:
            output.write(json.dumps(event, ensure_ascii=False) + "\n")
    # Preserve the established recovery layout for the paired input snapshots.
    for variant in ("control", "forget"):
        shutil.copyfile(
            directory / variant / (variant + ".snapshot.sqlite3"),
            directory / (variant + ".snapshot.sqlite3"),
        )
    return records, {
        "instance_id": instance.instance_id,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "state_directory": str(directory),
        "system_errors": sum(e["operation"] == "system_error" for e in events),
        "maintenance_errors": sum(bool(e.get("maintenance_error")) for e in events),
        "usage": usage,
        "usage_scope": "recovered_probes_only" if restore_from else "complete_instance",
    }


def _run_instance(
    benchmark,
    settings,
    judge_config,
    instance,
    items,
    directory,
    *,
    restore_from=None,
    branch_workers=1,
):
    if branch_workers == 2:
        return _run_parallel_pair(
            benchmark, settings, judge_config, instance, items, directory, restore_from
        )
    import faiss

    # Small flat indexes do not benefit from an OpenMP team per evaluation worker.
    faiss.omp_set_num_threads(1)
    started = time.perf_counter()
    with ExitStack() as stack:
        llm = stack.enter_context(LLMClient(settings.llm))
        embedding = stack.enter_context(EmbeddingClient(settings.embedding))
        judge_client = stack.enter_context(LLMClient(judge_config))
        if restore_from is None:
            adapter = MemoryDMAdapter(settings, llm, embedding, directory)
        else:
            adapter = RestoredAdapter(
                settings, llm, embedding, directory, restore_from=restore_from
            )
        stack.callback(adapter.close)
        try:
            records = benchmark.runner.run_instance(
                adapter, instance, items, CheckedJudge(benchmark, judge_client)
            )
        finally:
            usage = {
                "llm": asdict(llm.usage()),
                "embedding": asdict(embedding.usage()),
                "judge": asdict(judge_client.usage()),
            }
            _write_json(directory / "usage.json", usage)
    errors = [event for event in adapter.events if event["operation"] == "system_error"]
    maintenance = [event for event in adapter.events if event.get("maintenance_error")]
    return records, {
        "instance_id": instance.instance_id,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "state_directory": str(directory),
        "system_errors": len(errors),
        "maintenance_errors": len(maintenance),
        "usage": usage,
        "usage_scope": "recovered_probes_only" if restore_from else "complete_instance",
    }


def write_report(metrics: dict, meta: dict, path: Path) -> None:
    def percentage(value):
        return "--" if value is None else f"{value * 100:.2f}%"

    lines = [
        "# MemoryDM Bemula evaluation",
        "",
        f"Status: {meta['status']}",
        f"Instances completed: {meta['completed_instances']}/{meta['selected_instances']}",
        f"Agent: {meta['configuration']['settings']['llm']['model']}",
        f"Judge: {meta['configuration']['judge']['model']}",
        f"Dataset: {meta['configuration']['dataset']}",
        "",
        "Native MemoryDM answering and maintenance; official Bemula runner, judge and metrics.",
        "Scores cover completed instances in the selected dataset, not the full benchmark.",
        "",
        "| Metric | Score |",
        "| --- | --- |",
    ]
    for name, value in (
        ("RA", metrics["RA"]),
        ("FE", metrics["FE"]),
        ("PR", metrics["PR"]),
        ("RLA", metrics["RLA"]),
        ("AR", metrics["AR"]["average"]),
        ("RPR", metrics["request_pass_rate"]),
        ("Bemula Score", metrics["bemula_score"]),
    ):
        lines.append(f"| {name} | {percentage(value)} |")
    lines += [
        "",
        "## By forget request type",
        "",
        "| Type | RA | FE | RLA | AR |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, block in metrics["by_forget_type"].items():
        values = (block["RA"], block["FE"], block["RLA"], block["AR"]["average"])
        lines.append("| " + name + " | " + " | ".join(map(percentage, values)) + " |")
    lines += ["", "## Execution diagnostics", ""]
    for result in meta["instances"]:
        lines.append(
            f"- {result['instance_id']}: {result['system_errors']} system errors; "
            f"{result['maintenance_errors']} maintenance errors."
        )
    if meta["failures"]:
        lines += ["", "Incomplete instances are listed in run_meta.json; resume before comparison."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_variant_arguments(parser)
    parser.add_argument("--bemula-root", type=Path, help="Optional external Bemula evaluator checkout")
    parser.add_argument("--dataset", default="datasets/bemula-lite", help="Directory containing dialogues.jsonl and questions.jsonl")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--judge-model")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--branch-workers",
        type=int,
        choices=(1, 2),
        default=1,
        help="Concurrent control/forget branches per instance",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--recover-from",
        type=Path,
        help="Reuse valid checkpoints and completed ingestion from a prior run",
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate inputs without API calls")
    args = parser.parse_args(argv)
    if args.workers < 1 or (args.limit is not None and args.limit < 1):
        parser.error("--workers and --limit must be positive")
    if args.resume and args.out is None:
        parser.error("--resume requires --out")
    if args.recover_from and (args.out is None or args.resume):
        parser.error("--recover-from requires a fresh --out and cannot combine with --resume")
    root = args.bemula_root.resolve() if args.bemula_root else Path(__file__).with_name("_vendor")
    benchmark = load_benchmark(args.bemula_root)
    dataset = Path(args.dataset)
    if args.bemula_root and not dataset.is_absolute() and not dataset.is_dir():
        dataset = root / "data" / dataset
    dialogues = dataset.resolve() / "dialogues.jsonl"
    questions = dialogues.with_name("questions.jsonl")
    instances, by_instance = _load_data(benchmark, dialogues, questions, args.limit)
    counts = {
        "instances": len(instances),
        "questions": sum(len(by_instance[i.instance_id]) for i in instances),
        "probes": sum(len(expected_probes(by_instance[i.instance_id])) for i in instances),
        "turns_per_variant": sum(len(s.turns) for i in instances for s in i.sessions),
        "forget_types": dict(
            Counter(r.forget_type.value for i in instances for r in i.forget_requests)
        ),
    }
    print(json.dumps(counts, indent=2), flush=True)
    if args.dry_run:
        return 0
    settings = apply_variant(
        Settings.from_env(args.env_file), args.variant, with_derived=args.with_derived
    )
    values = {**dotenv_values(args.env_file), **os.environ}
    judge_config = judge_api_config(settings.llm, values, "BEMULA_JUDGE", args.judge_model)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    out = args.out or Path("data/bemula") / dataset.name / stamp
    out.mkdir(parents=True, exist_ok=True)
    code_root = Path(__file__).resolve().parents[1]
    judge_meta = asdict(judge_config)
    judge_meta.pop("api_key")
    configuration = {
        "protocol": "bemula_native_v1",
        "settings": _settings_metadata(settings),
        "judge": judge_meta,
        "judge_temperature": 0.0,
        "judge_max_tokens": 16384,
        "instance_ids": [i.instance_id for i in instances],
        "dataset": args.dataset,
        "inputs": {str(p): _digest(p) for p in (dialogues, questions)},
        "memory_dm_source": {
            str(p.relative_to(code_root)): _digest(p)
            for p in sorted(code_root.rglob("*"))
            if p.suffix in (".py", ".md")
        },
        "bemula_source": {
            str(p.relative_to(root)): _digest(p) for p in sorted((root / "bemula").rglob("*.py"))
        },
    }
    manifest_path = out / "run_meta.json"
    previous = json.loads(manifest_path.read_text()) if manifest_path.is_file() else None
    if previous is not None:
        if not args.resume:
            raise ValueError("Output already has a run; use --resume or a fresh --out directory")
        if previous["configuration"] != configuration:
            raise ValueError("Cannot resume: dataset, source code, models or settings changed")
    checkpoints = out / "checkpoints"
    checkpoints.mkdir(exist_ok=True)
    restores = {}
    if args.recover_from:
        if previous is not None:
            raise ValueError("Recovery requires a fresh output directory")
        old_meta = json.loads((args.recover_from / "run_meta.json").read_text())
        check_compatible(old_meta["configuration"], configuration)
        for checkpoint in (args.recover_from / "checkpoints").glob("*.json"):
            shutil.copyfile(checkpoint, checkpoints / checkpoint.name)
        restores = prepare_snapshots(args.recover_from, out / "recovery")
    completed, diagnostics = {}, {}
    for index, instance in enumerate(instances):
        checkpoint = checkpoints / f"{index:04d}.json"
        if (args.resume or args.recover_from) and checkpoint.is_file():
            data = json.loads(checkpoint.read_text())
            records = [benchmark.runner.ItemRecord.model_validate(r) for r in data["records"]]
            keys = [(record.item_id, record.variant) for record in records]
            if (
                len(set(keys)) != len(keys)
                or set(keys) != expected_probes(by_instance[instance.instance_id])
                or any(r.instance_id != instance.instance_id for r in records)
            ):
                raise ValueError("Incomplete or incompatible instance checkpoint")
            completed[instance.instance_id] = records
            diagnostics[instance.instance_id] = data["diagnostics"]
    meta = {
        "status": "running",
        "configuration": configuration,
        "counts": counts,
        "started_at": previous["started_at"] if previous else datetime.now(UTC).isoformat(),
        "memory_dm_commit": _git(Path.cwd(), "rev-parse", "HEAD"),
        "memory_dm_dirty": bool(_git(Path.cwd(), "status", "--porcelain")),
        "bemula_commit": _git(root, "rev-parse", "HEAD"),
        "bemula_dirty": bool(_git(root, "status", "--porcelain")),
        "selected_instances": len(instances),
        "completed_instances": len(completed),
        "instances": list(diagnostics.values()),
        "failures": [],
        "workers": args.workers,
        "branch_workers": args.branch_workers,
        "max_branch_concurrency": args.workers * args.branch_workers,
        "recovered_from": str(args.recover_from) if args.recover_from else None,
    }
    _write_json(manifest_path, meta)
    with ThreadPoolExecutor(max_workers=min(args.workers, len(instances))) as pool:
        futures = {
            pool.submit(
                _run_instance,
                benchmark,
                settings,
                judge_config,
                instance,
                by_instance[instance.instance_id],
                out / "states" / uuid4().hex,
                **({"branch_workers": args.branch_workers} if args.branch_workers == 2 else {}),
                **(
                    {"restore_from": restores[instance.instance_id]}
                    if instance.instance_id in restores
                    else {}
                ),
            ): (index, instance)
            for index, instance in enumerate(instances)
            if instance.instance_id not in completed
        }
        for future in as_completed(futures):
            index, instance = futures[future]
            try:
                records, diagnostic = future.result()
                keys = [(record.item_id, record.variant) for record in records]
                if len(set(keys)) != len(keys) or set(keys) != expected_probes(
                    by_instance[instance.instance_id]
                ):
                    raise ValueError("Official runner did not produce the complete probe set")
                _write_json(
                    checkpoints / f"{index:04d}.json",
                    {
                        "records": [record.model_dump(mode="json") for record in records],
                        "diagnostics": diagnostic,
                    },
                )
                completed[instance.instance_id] = records
                diagnostics[instance.instance_id] = diagnostic
                print(f"Completed {instance.instance_id}: {len(records)} probes", flush=True)
            except Exception as error:
                meta["failures"].append(
                    {
                        "instance_id": instance.instance_id,
                        "error_type": type(error).__name__,
                        "message": str(error),
                    }
                )
                print(f"Failed {instance.instance_id}: {type(error).__name__}", flush=True)
            meta["completed_instances"] = len(completed)
            meta["instances"] = list(diagnostics.values())
            _write_json(manifest_path, meta)
    records = [
        record for instance in instances for record in completed.get(instance.instance_id, [])
    ]
    with (out / "items.jsonl").open("w", encoding="utf-8") as output:
        for record in records:
            output.write(record.model_dump_json() + "\n")
    metrics = benchmark.metrics.compute_metrics(records)
    _write_json(out / "metrics.json", metrics)
    meta["status"] = "partial" if meta["failures"] else "completed"
    if not meta["failures"] and any(
        d["system_errors"] or d["maintenance_errors"] for d in diagnostics.values()
    ):
        meta["status"] = "completed_with_system_errors"
    meta["finished_at"] = datetime.now(UTC).isoformat()
    _write_json(manifest_path, meta)
    write_report(metrics, meta, out / "report.md")
    print(f"Report: {out / 'report.md'}", flush=True)
    return 1 if meta["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
