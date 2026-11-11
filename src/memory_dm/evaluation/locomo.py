"""Evaluate native MemoryDM on LoCoMo with isolated, resumable conversation states."""

import argparse
import hashlib
import json
import shutil
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from memory_dm.config import Settings
from memory_dm.evaluation.common import digest, git, settings_metadata, write_json
from memory_dm.evaluation.ingestion_reuse import reusable_ingestion
from memory_dm.evaluation.locomo_data import CATEGORIES, Question, Sample, load_dataset
from memory_dm.evaluation.locomo_metrics import (
    aggregate,
    score_bleu1,
    score_prediction,
    scorer_metadata,
)
from memory_dm.evaluation.variants import add_variant_arguments, apply_variant
from memory_dm.llm.clients import EmbeddingClient, LLMClient
from memory_dm.memory.agent import MemoryAgent
from memory_dm.memory.state import derived_failures
from memory_dm.models import Turn
from memory_dm.storage.sqlite import SQLiteStore

UNKNOWN = "Not mentioned in the conversation"
PROTOCOL = "locomo_native_qa_v3"
QUERY_ATTEMPTS = 2


class AnswerClient:
    """Apply benchmark answer formatting only after native retrieval has finished.

    Query planning and embeddings must see the original question, without a
    repeated formatting instruction or the adversarial distractor as search terms.
    Structured memory/retrieval calls are delegated unchanged to the real client.
    """

    def __init__(self, client: LLMClient, prompt: str):
        self.client, self.prompt = client, prompt
        self.config = client.config

    def usage(self):
        return self.client.usage()

    def chat_json(self, *args, **kwargs):
        return self.client.chat_json(*args, **kwargs)

    def chat(self, turns: list[Turn], **kwargs):
        if not turns or turns[-1].role != "user":
            raise ValueError("LoCoMo answering requires a final question turn")
        messages = [*turns[:-1], turns[-1].model_copy(update={"content": self.prompt})]
        return self.client.chat(messages, **kwargs)


def query_prompt(sample_id: str, question: Question, seed: int) -> tuple[str, list[str]]:
    prompt = question.question
    choices = []
    if question.category == 2:
        prompt += (
            "\nUse the date of the source conversation to resolve relative dates, "
            "including relative words such as yesterday or next week. "
            "Reply with the date alone, without explanation, citation or parenthetical context."
        )
    if question.category == 5:
        choices = [UNKNOWN, question.distractor]
        # Stable across processes, worker schedules and resumed runs.
        key = f"{seed}:{sample_id}:{question.index}".encode()
        if hashlib.sha256(key).digest()[0] % 2:
            choices.reverse()
        prompt += f"\nSelect the correct answer: (a) {choices[0]} (b) {choices[1]}."
    prompt += (
        "\nAnswer with the shortest complete phrase, using the conversation's wording when "
        "possible. Do not add explanations, caveats or parenthetical context. "
        "Write every date as D Month YYYY (for example 7 May 2023), never in ISO form. "
        "If the answer is unavailable, say 'No information available'."
    )
    return prompt, choices


def scored_answer(prediction: str, choices: list[str]) -> str:
    if choices:
        # Resolve only unambiguous option-only replies; never guess a malformed choice.
        option = prediction.strip().lower().rstrip(".")
        if option in ("a", "(a)", "b", "(b)"):
            return choices[0 if "a" in option else 1]
    return prediction


def _question_identity(sample: Sample, question: Question) -> dict:
    return {
        "sample_id": sample.sample_id,
        "question_index": question.index,
        "question": question.question,
        "category": question.category,
        "reference": question.answer,
        "evidence": list(question.evidence),
    }


def _completed_record(path: Path, sample: Sample, question: Question, seed: int) -> dict | None:
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    if any(record.get(k) != v for k, v in _question_identity(sample, question).items()):
        raise ValueError("Incompatible LoCoMo question checkpoint")
    if record.get("status") != "completed":
        return None
    prompt, choices = query_prompt(sample.sample_id, question, seed)
    prediction = record.get("prediction")
    if not isinstance(prediction, str) or not prediction.strip() or record.get("prompt") != prompt:
        raise ValueError("Incomplete LoCoMo question checkpoint")
    expected = score_prediction(scored_answer(prediction, choices), question)
    if record.get("score") != expected:
        raise ValueError("LoCoMo question checkpoint has an inconsistent score")
    expected_bleu = score_bleu1(scored_answer(prediction, choices), question)
    if record.get("bleu1") != expected_bleu:
        record["bleu1"] = expected_bleu
        write_json(path, record)
    return record


def _ingest(sample: Sample, settings, llm, embedding, directory: Path) -> dict:
    marker = directory / "ingestion.json"
    snapshot = directory / "snapshot.sqlite3"
    if marker.exists():
        result = json.loads(marker.read_text(encoding="utf-8"))
        if (
            result.get("sample_id") != sample.sample_id
            or not snapshot.is_file()
            or result.get("snapshot_sha256") != digest(snapshot)
        ):
            raise ValueError("Incomplete or changed LoCoMo ingestion snapshot")
        return {**result, "reused": True}
    started = time.perf_counter()
    attempt = directory / ("ingest-" + uuid4().hex)
    attempt.mkdir(parents=True)
    before_llm = llm.usage()
    before_embedding = embedding.usage() if embedding else None
    warnings = []
    rejections = {"candidates": 0, "groups": 0, "reasons": {}}

    def warn(source, stage, error_type, *, turn_id=None, diagnostics=None):
        event = {
            "session_number": source.number,
            "stage": stage,
            "error_type": error_type,
            "diagnostics": diagnostics or {},
        }
        if turn_id is not None:
            event["turn_id"] = turn_id
        warnings.append(event)
        with (attempt / "maintenance.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(event, ensure_ascii=False) + "\n")

    def report_warnings(source, report, turn_id=None):
        if report is not None and report.experience is not None:
            experience = report.experience
            rejections["candidates"] += experience.rejected_candidates
            rejections["groups"] += experience.rejected_groups
            for code, count in experience.rejection_reasons.items():
                rejections["reasons"][code] = rejections["reasons"].get(code, 0) + count
        for failure in derived_failures(report):
            warn(
                source,
                failure["stage"],
                failure["error_type"],
                turn_id=turn_id,
                diagnostics=failure.get("diagnostics"),
            )
        if report is not None:
            for failure in report.recovery_failures:
                warn(
                    source,
                    "atomic_recovery",
                    failure["error_type"],
                    diagnostics=failure.get("diagnostics"),
                )

    with SQLiteStore(attempt / "memory.sqlite3") as store:
        agent = MemoryAgent(store, settings, llm, embedding)
        # Each sample is an independent two-person archive, not two user accounts.
        for source in sample.sessions:
            session = store.create_session("locomo-user")
            for turn in source.turns:
                message = Turn(
                    session_id=session.session_id,
                    role="user",
                    content=turn.content(),
                    turn_id=f"locomo:{sample.sample_id}:{turn.dia_id}",
                    timestamp=source.timestamp,
                )
                try:
                    result = agent.observe(session, message)
                except Exception as error:
                    warn(
                        source,
                        "observe",
                        type(error).__name__,
                        turn_id=message.turn_id,
                        diagnostics=getattr(error, "diagnostics", {}),
                    )
                    # The benchmark input is a source archive. If routing failed
                    # before persistence, retain that source for native RAW retrieval.
                    # Product chat's deletion-request policy is not changed here.
                    if store.get_turn(message.turn_id, "locomo-user") is None:
                        store.add_turn(message, "locomo-user")
                    continue
                if result.maintenance_error:
                    warn(
                        source,
                        "atomic",
                        "MAINTENANCE_ERROR",
                        turn_id=message.turn_id,
                        diagnostics=result.maintenance_diagnostics,
                    )
                report_warnings(source, result.write, message.turn_id)
            try:
                report_warnings(source, agent.end_session(session))
            except Exception as error:
                warn(
                    source,
                    "session_end",
                    type(error).__name__,
                    diagnostics=getattr(error, "diagnostics", {}),
                )
            print(f"{sample.sample_id}: ingested session {source.number}", flush=True)
        pending = {
            "atomic_turns": store.connection.execute(
                "SELECT count(*) FROM conversation_turns t LEFT JOIN memory_processed_turns p "
                "ON p.turn_id=t.turn_id WHERE p.turn_id IS NULL"
            ).fetchone()[0],
            "experience_sources": store.connection.execute(
                "SELECT count(*) FROM experience_pending"
            ).fetchone()[0],
            "profile_failed": bool(
                store.connection.execute(
                    "SELECT 1 FROM profile_progress WHERE status != 'completed' LIMIT 1"
                ).fetchone()
            ),
        }
        source_turns_stored = store.connection.execute(
            "SELECT count(*) FROM conversation_turns"
        ).fetchone()[0]
        # All source messages were attempted; incomplete derived maintenance is
        # observable degradation, not a reason to skip the answering workload.
        with sqlite3.connect(snapshot) as target:
            store.connection.backup(target)
    result = {
        "sample_id": sample.sample_id,
        "snapshot_sha256": digest(snapshot),
        "sessions": len(sample.sessions),
        "turns": sum(len(s.turns) for s in sample.sessions),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "usage": {"llm": asdict(llm.usage().since(before_llm))},
        "maintenance_warnings": warnings,
        "maintenance_warning_count": len(warnings),
        "maintenance_pending": pending,
        "experience_rejections": rejections,
        "source_turns_stored": source_turns_stored,
    }
    if embedding:
        result["usage"]["embedding"] = asdict(embedding.usage().since(before_embedding))
    write_json(marker, result)
    return {**result, "reused": False}


def _answer_questions(sample, settings, directory, seed, llm, embedding, store):
    diagnostic = {"failures": [], "query_seconds_this_invocation": 0.0}
    reference_time = sample.sessions[-1].timestamp
    for question in sample.questions:
        path = directory / "questions" / f"{question.index:04d}.json"
        if _completed_record(path, sample, question, seed) is not None:
            continue
        prompt, choices = query_prompt(sample.sample_id, question, seed)
        record = {
            **_question_identity(sample, question),
            "prompt": prompt,
            "status": "error",
            "prediction": "",
            "score": 0.0,
            "bleu1": None if question.category == 5 else 0.0,
        }
        query_started = time.perf_counter()
        for attempt in range(1, QUERY_ATTEMPTS + 1):
            try:
                agent = MemoryAgent(store, settings, AnswerClient(llm, prompt), embedding)
                reply = agent.query(
                    "locomo-user", question.question, reference_time=reference_time
                )
                if not reply.content.strip():
                    raise ValueError("LoCoMo query returned an empty answer")
            except Exception as error:
                # Keep the message: the error type alone cannot distinguish a planner,
                # evidence, embedding or grep failure when the run is analysed later.
                record["error_type"] = type(error).__name__
                record["error"] = str(error)[:300]
                if attempt < QUERY_ATTEMPTS:
                    time.sleep(0.5 * attempt)
                    continue
                diagnostic["failures"].append(
                    {
                        "question_index": question.index,
                        "error_type": type(error).__name__,
                        "error": str(error)[:300],
                        "attempts": attempt,
                    }
                )
                break
            record.pop("error_type", None)
            record.pop("error", None)
            record.update(
                status="completed",
                prediction=reply.content,
                metrics=reply.metrics,
                score=score_prediction(scored_answer(reply.content, choices), question),
                bleu1=score_bleu1(scored_answer(reply.content, choices), question),
            )
            break
        record["elapsed_seconds"] = round(time.perf_counter() - query_started, 3)
        diagnostic["query_seconds_this_invocation"] += record["elapsed_seconds"]
        write_json(path, record)
    return diagnostic


def _query_batch(sample, settings, directory, seed):
    """Each worker owns its transport, usage counters, mutable caches and SQLite copy."""
    import faiss

    faiss.omp_set_num_threads(1)
    with ExitStack() as stack:
        llm = stack.enter_context(LLMClient(settings.llm))
        embedding = (
            stack.enter_context(EmbeddingClient(settings.embedding))
            if settings.embedding.model.strip() and settings.embedding.api_key.strip()
            else None
        )
        query_db = directory / ("query-" + uuid4().hex + ".sqlite3")
        shutil.copyfile(directory / "snapshot.sqlite3", query_db)
        store = stack.enter_context(SQLiteStore(query_db))
        result = _answer_questions(sample, settings, directory, seed, llm, embedding, store)
        result["usage"] = {"llm": asdict(llm.usage())}
        if embedding:
            result["usage"]["embedding"] = asdict(embedding.usage())
        return result


def run_sample(
    sample: Sample,
    settings: Settings,
    directory: Path,
    seed: int,
    shared_ingestion: Path | None = None,
    question_workers: int = 1,
    reused_ingestion: dict | None = None,
) -> dict:
    import faiss

    faiss.omp_set_num_threads(1)
    directory.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    diagnostic = {"sample_id": sample.sample_id, "status": "error", "failures": []}
    diagnostic["query_seconds_this_invocation"] = 0.0
    query_usage = {}
    with ExitStack() as stack:
        llm = stack.enter_context(LLMClient(settings.llm))
        embedding = (
            stack.enter_context(EmbeddingClient(settings.embedding))
            if settings.embedding.model.strip() and settings.embedding.api_key.strip()
            else None
        )
        try:
            if reused_ingestion is not None:
                source = Path(reused_ingestion["snapshot"])
                imported = reused_ingestion["ingestion"]
                if digest(source) != imported["snapshot_sha256"]:
                    raise ValueError("Reusable ingestion changed after validation")
                local = directory / "snapshot.sqlite3"
                if local.exists() and digest(local) != imported["snapshot_sha256"]:
                    raise ValueError("Local snapshot differs from reusable ingestion")
                if not local.exists():
                    shutil.copyfile(source, local)
                write_json(directory / "ingestion.json", imported)
            ingestion_directory = directory
            if shared_ingestion is not None:
                # Content-address the source and ingestion configuration. Retrieval-only
                # overrides must share the same imported state.
                identity_settings = replace(
                    settings, retrieval_methods=("hybrid", "grep", "time"), retrieval_max_rounds=2
                )
                identity = json.dumps(
                    {
                        "sample": asdict(sample),
                        "source": {
                            str(p): digest(p)
                            for p in sorted(Path(__file__).parents[1].rglob("*.py"))
                        },
                        "settings": settings_metadata(identity_settings),
                    },
                    sort_keys=True,
                    default=str,
                )
                key = hashlib.sha256(identity.encode()).hexdigest()
                ingestion_directory = shared_ingestion / key
                ingestion_directory.mkdir(parents=True, exist_ok=True)
            import fcntl

            ingestion_directory.mkdir(parents=True, exist_ok=True)
            with (ingestion_directory / ".ingestion.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                diagnostic["ingestion"] = _ingest(
                    sample, settings, llm, embedding, ingestion_directory
                )
            if ingestion_directory != directory:
                local = directory / "snapshot.sqlite3"
                if not local.exists():
                    shutil.copyfile(ingestion_directory / "snapshot.sqlite3", local)
                elif digest(local) != digest(ingestion_directory / "snapshot.sqlite3"):
                    raise ValueError("Local snapshot differs from shared ingestion")
            query_db = directory / ("query-" + uuid4().hex + ".sqlite3")
            shutil.copyfile(directory / "snapshot.sqlite3", query_db)
            store = stack.enter_context(SQLiteStore(query_db))
            pending = tuple(
                q
                for q in sample.questions
                if _completed_record(
                    directory / "questions" / f"{q.index:04d}.json", sample, q, seed
                )
                is None
            )
            if question_workers > 1 and pending:
                workers = min(question_workers, len(pending))
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [
                        pool.submit(
                            _query_batch,
                            replace(sample, questions=pending[i::workers]),
                            settings,
                            directory,
                            seed,
                        )
                        for i in range(workers)
                    ]
                    results = [future.result() for future in as_completed(futures)]
                for result in results:
                    diagnostic["failures"].extend(result["failures"])
                    diagnostic["query_seconds_this_invocation"] += result[
                        "query_seconds_this_invocation"
                    ]
                    for kind, values in result["usage"].items():
                        totals = query_usage.setdefault(kind, {})
                        for field, value in values.items():
                            totals[field] = totals.get(field, 0) + value
            else:
                result = _answer_questions(sample, settings, directory, seed, llm, embedding, store)
                diagnostic["failures"].extend(result["failures"])
                diagnostic["query_seconds_this_invocation"] += result[
                    "query_seconds_this_invocation"
                ]
            diagnostic["status"] = (
                "partial"
                if diagnostic["failures"]
                else "completed_with_warnings"
                if diagnostic["ingestion"].get("maintenance_warning_count")
                else "completed"
            )
        except Exception as error:
            # Persist safe error types, not provider responses or data-bearing exceptions.
            diagnostic["failures"].append({"stage": "sample", "error_type": type(error).__name__})
        finally:
            diagnostic["elapsed_seconds"] = round(time.perf_counter() - started, 3)
            diagnostic["usage_this_invocation"] = {"llm": asdict(llm.usage())}
            if embedding:
                diagnostic["usage_this_invocation"]["embedding"] = asdict(embedding.usage())
            for kind, values in query_usage.items():
                for field, value in values.items():
                    diagnostic["usage_this_invocation"][kind][field] += value
            write_json(directory / "result.json", diagnostic)
    return diagnostic


def _report(metrics: dict, meta: dict, path: Path) -> None:
    def percentage(value):
        return "--" if value is None else f"{value * 100:.2f}%"

    lines = [
        "# MemoryDM LoCoMo evaluation",
        "",
        f"Status: {meta['status']}",
        f"Model: {meta['configuration']['settings']['llm']['model']}",
        f"Questions scored: {metrics['scored_questions']}/{metrics['expected_questions']}",
        f"Failed questions (scored zero): {metrics['failed_questions']}",
        f"Maintenance warning events: {metrics['maintenance_warning_events']}",
        "Pending maintenance: " + json.dumps(metrics["maintenance_pending"]),
        "Rejected experience proposals: " + json.dumps(metrics["experience_rejections"]),
        "",
        "Official-compatible QA scorer; native MemoryDM ingestion and read-only answering.",
        "F1 covers categories 1–4; adversarial accuracy is reported separately.",
        "Partial or limited runs are not full-dataset results.",
        "",
        f"F1: {percentage(metrics['f1'])}",
        f"BLEU-1: {percentage(metrics['bleu1'])}",
        f"Adversarial accuracy: {percentage(metrics['adversarial_accuracy'])}",
        f"Combined official score: {percentage(metrics['overall_official_score'])}",
        "",
        "| Category | Metric | Questions | Score |",
        "| --- | --- | ---: | ---: |",
    ]
    for value in metrics["by_category"].values():
        lines.append(
            f"| {value['name']} | {value['metric']} | {value['count']} | "
            f"{percentage(value['mean_score'])} |"
        )
    cost = metrics["retrieval_cost"]

    def number(value):
        return "--" if value is None else f"{value:.3f}"

    lines += [
        "", "## Retrieval cost", "",
        "Scope: all selected categories; sum saved retrieval traces per question, "
        "then average over questions with traces.",
        f"Questions with retrieval metrics: {cost['measured_questions']}/"
        f"{cost['expected_questions']} (missing: {cost['missing_questions']}).",
        f"Mean retrieval LLM calls per question: {number(cost['mean_llm_calls'])}",
        f"Mean retrieval LLM input + output tokens per question: "
        f"{number(cost['mean_llm_tokens'])}",
        f"Mean retrieval latency (seconds): {number(cost['mean_latency_seconds'])}",
        f"Retrieval LLM calls missing token usage: {cost['totals']['llm_usage_missing']}",
        "Excludes ingestion, final-answer generation and embedding usage. "
        "Saved traces do not include discarded failed question attempts. "
        "Missing traces are not counted as zero cost; unavailable token means are shown as --.",
        "", "## Execution", "",
    ]
    for result in meta["samples"]:
        lines.append(
            f"- {result['sample_id']}: {result['status']}, "
            f"{result.get('elapsed_seconds', 0):.1f}s this invocation; "
            f"ingestion reused: {result.get('ingestion', {}).get('reused', False)}."
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_variant_arguments(parser)
    parser.add_argument("--dataset", type=Path, required=True, help="Path to locomo10.json")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--shared-ingestion", type=Path, help="Reuse imported states across retrieval variants"
    )
    parser.add_argument(
        "--reuse-ingestion-from", type=Path,
        help="Reuse verified snapshots from an existing LoCoMo run; never re-import",
    )
    parser.add_argument(
        "--workers", type=int, default=1, help="Concurrent independent conversations"
    )
    parser.add_argument(
        "--question-workers",
        type=int,
        default=1,
        help="Concurrent independent question workers per conversation (isolated DBs and clients)",
    )
    parser.add_argument(
        "--limit", type=int, help="Evaluate only the first N selected conversations"
    )
    parser.add_argument("--sample-id", nargs="+", help="Select original sample IDs")
    parser.add_argument(
        "--max-questions", type=int, help="Debug: first N selected QAs per conversation"
    )
    parser.add_argument(
        "--categories", type=int, nargs="+", choices=CATEGORIES, default=list(CATEGORIES)
    )
    parser.add_argument("--timezone", default="UTC", help="Assumed timezone of naive source dates")
    parser.add_argument("--seed", type=int, default=0, help="Seed for adversarial option order")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Validate data without API calls")
    args = parser.parse_args(argv)
    if args.reuse_ingestion_from and args.shared_ingestion:
        parser.error("Use either --reuse-ingestion-from or --shared-ingestion")
    if (
        args.workers < 1
        or args.question_workers < 1
        or any(v is not None and v < 1 for v in (args.limit, args.max_questions))
    ):
        parser.error("Worker counts, --limit and --max-questions must be positive")
    if args.resume and args.out is None:
        parser.error("--resume requires --out")
    dataset = args.dataset.resolve()
    samples = load_dataset(dataset, timezone=args.timezone)
    if args.sample_id:
        unknown = set(args.sample_id) - {s.sample_id for s in samples}
        if unknown:
            raise ValueError(f"Unknown LoCoMo sample IDs: {sorted(unknown)}")
        samples = [s for s in samples if s.sample_id in args.sample_id]
    samples = samples[: args.limit] if args.limit else samples
    samples = [
        replace(
            s,
            questions=tuple(q for q in s.questions if q.category in args.categories)[
                : args.max_questions
            ],
        )
        for s in samples
    ]
    if not samples or any(not s.questions for s in samples):
        raise ValueError("Every selected LoCoMo conversation must have selected questions")
    counts = {
        "samples": len(samples),
        "sessions": sum(len(s.sessions) for s in samples),
        "turns": sum(len(session.turns) for s in samples for session in s.sessions),
        "questions": sum(len(s.questions) for s in samples),
        "by_category": {
            str(c): sum(q.category == c for s in samples for q in s.questions) for c in CATEGORIES
        },
    }
    print(json.dumps(counts, indent=2), flush=True)
    if args.dry_run:
        return 0
    scorer = scorer_metadata()
    settings = apply_variant(
        Settings.from_env(args.env_file), args.variant, with_derived=args.with_derived
    )
    if not settings.auto_memory:
        raise ValueError("LoCoMo native evaluation requires MEMORY_DM_AUTO_MEMORY=true")
    reused = (
        reusable_ingestion(args.reuse_ingestion_from, samples, settings, dataset, args.timezone)
        if args.reuse_ingestion_from else None
    )
    code_root = Path(__file__).resolve().parents[1]
    configuration = {
        "protocol": PROTOCOL,
        "scorer": scorer,
        "dataset": str(dataset),
        "dataset_sha256": digest(dataset),
        "settings": settings_metadata(settings),
        "timezone": args.timezone,
        "seed": args.seed,
        "selection": {s.sample_id: [q.index for q in s.questions] for s in samples},
        "memory_dm_source": {
            str(p.relative_to(code_root)): digest(p)
            for p in sorted(code_root.rglob("*"))
            if p.suffix in (".py", ".md")
        },
    }
    if reused is not None:
        configuration["reused_ingestion"] = reused
    out = args.out or Path("data/locomo") / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    manifest = out / "run_meta.json"
    old = json.loads(manifest.read_text()) if manifest.is_file() else None
    if old is not None and (not args.resume or old["configuration"] != configuration):
        raise ValueError(
            "Cannot resume: use --resume with unchanged data, source, settings and selection"
        )
    if old is None and args.resume:
        raise ValueError("Cannot resume: output has no LoCoMo run manifest")
    if old is None and out.exists() and any(out.iterdir()):
        raise ValueError("LoCoMo output must be empty for a fresh run")
    meta = {
        "status": "running",
        "configuration": configuration,
        "counts": counts,
        "started_at": old["started_at"] if old else datetime.now(UTC).isoformat(),
        "invocation_started_at": datetime.now(UTC).isoformat(),
        "workers": args.workers,
        "question_workers": args.question_workers,
        "max_query_concurrency": min(args.workers, len(samples)) * args.question_workers,
        "memory_dm_commit": git(Path.cwd(), "rev-parse", "HEAD"),
        "memory_dm_dirty": bool(git(Path.cwd(), "status", "--porcelain")),
        "samples": [],
    }
    # Validate all reusable answers before making any new API requests.
    for index, sample in enumerate(samples):
        for question in sample.questions:
            _completed_record(
                out / "states" / f"{index:04d}" / "questions" / f"{question.index:04d}.json",
                sample,
                question,
                args.seed,
            )
    write_json(manifest, meta)
    with ThreadPoolExecutor(max_workers=min(args.workers, len(samples))) as pool:
        futures = {
            pool.submit(
                run_sample,
                sample,
                settings,
                out / "states" / f"{index:04d}",
                args.seed,
                args.shared_ingestion,
                args.question_workers,
                reused["samples"][sample.sample_id] if reused else None,
            ): sample.sample_id
            for index, sample in enumerate(samples)
        }
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as error:
                result = {
                    "sample_id": futures[future],
                    "status": "error",
                    "failures": [{"error_type": type(error).__name__}],
                }
            meta["samples"].append(result)
            print(f"{result['sample_id']}: {result['status']}", flush=True)
            write_json(manifest, meta)
    records = []
    for index, sample in enumerate(samples):
        for question in sample.questions:
            path = out / "states" / f"{index:04d}" / "questions" / f"{question.index:04d}.json"
            if path.exists():
                records.append(json.loads(path.read_text(encoding="utf-8")))
    metrics = aggregate(records, counts["questions"])
    metrics["maintenance_warning_events"] = sum(
        s.get("ingestion", {}).get("maintenance_warning_count", 0) for s in meta["samples"]
    )
    metrics["samples_with_maintenance_warnings"] = sum(
        bool(s.get("ingestion", {}).get("maintenance_warning_count", 0)) for s in meta["samples"]
    )
    metrics["maintenance_pending"] = {
        field: sum(
            s.get("ingestion", {}).get("maintenance_pending", {}).get(field, 0)
            for s in meta["samples"]
        )
        for field in ("atomic_turns", "experience_sources", "profile_failed")
    }
    metrics["experience_rejections"] = {"candidates": 0, "groups": 0, "reasons": {}}
    for sample_result in meta["samples"]:
        rejected = sample_result.get("ingestion", {}).get("experience_rejections", {})
        for field in ("candidates", "groups"):
            metrics["experience_rejections"][field] += rejected.get(field, 0)
        for code, count in rejected.get("reasons", {}).items():
            reasons = metrics["experience_rejections"]["reasons"]
            reasons[code] = reasons.get(code, 0) + count
    metrics["usage_this_invocation"] = {
        kind: {
            field: sum(
                s.get("usage_this_invocation", {}).get(kind, {}).get(field, 0)
                for s in meta["samples"]
            )
            for field in ("calls", "input_tokens", "output_tokens", "usage_missing")
        }
        for kind in ("llm", "embedding")
    }
    meta["status"] = (
        ("completed_with_warnings" if metrics["maintenance_warning_events"] else "completed")
        if (
            all(s["status"] in {"completed", "completed_with_warnings"} for s in meta["samples"])
            and metrics["successful_questions"] == counts["questions"]
        )
        else "partial"
    )
    meta["finished_at"] = datetime.now(UTC).isoformat()
    write_json(manifest, meta)
    write_json(out / "metrics.json", metrics)
    with (out / "items.jsonl").open("w", encoding="utf-8") as output:
        for record in records:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")
    _report(metrics, meta, out / "report.md")
    return 0 if meta["status"] in {"completed", "completed_with_warnings"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
