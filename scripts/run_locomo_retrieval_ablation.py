"""Run the published retrieval comparisons with shared ingestion."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--question-workers", type=int, default=4)
    parser.add_argument("--full-only", action="store_true")
    parser.add_argument("--skip-full", action="store_true")
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--prompt-source", type=Path, help="Optional alternative LightMem llm_judge.py")
    parser.add_argument("--source-dir", type=Path, help="Separate frozen source for new variants")
    parser.add_argument("--reuse-ingestion-from", type=Path)
    parser.add_argument(
        "--only", choices=(
            "full", "no_hybrid", "no_grep", "single_round",
        )
    )
    parser.add_argument("--wait-for-variant", action="store_true")
    parser.add_argument("--wait-for-full", action="store_true",
                        help="Wait for an existing Full run before resuming it")
    args = parser.parse_args()
    if args.full_only and args.skip_full:
        parser.error("--full-only and --skip-full cannot be combined")
    root = Path(__file__).resolve().parents[1]
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / "experiment.json"
    if args.wait_for_variant:
        if not args.only:
            parser.error("--wait-for-variant requires --only")
        variant_meta = out / args.only / "run_meta.json"
        while variant_meta.exists() and json.loads(variant_meta.read_text())["status"] == "running":
            time.sleep(10)
    if args.wait_for_full:
        while manifest.exists():
            previous = json.loads(manifest.read_text())
            if previous.get("variants", {}).get("full", {}).get("status") != "running":
                break
            time.sleep(10)
    # Keep imports and cache identities stable if the working tree changes mid-run.
    frozen = args.source_dir.resolve() if args.source_dir else out / "source"
    if not frozen.exists():
        shutil.copytree(root / "src", frozen, ignore=shutil.ignore_patterns("__pycache__"))
    env = {
        **os.environ,
        "PYTHONPATH": str(frozen),
        "PYTHONUNBUFFERED": "1",
        "OMP_NUM_THREADS": "1",
        "API_TIMEOUT": "180",
        "RETRIEVAL_METHODS": "hybrid,grep,time",
    }
    state = {
        "started_at": datetime.now(UTC).isoformat(),
        "categories": [1, 2, 3, 4],
        "env_file": str(args.env_file.resolve()),
        "workers": args.workers,
        "question_workers": args.question_workers,
        "judge": "LightMem accuracy prompt with the configured judge model",
        "variants": {},
    }
    if manifest.exists():
        state = json.loads(manifest.read_text())
    state["requested_scope"] = "full_only" if args.full_only else "all_variants"
    state["workers"] = args.workers
    state["question_workers"] = args.question_workers
    if args.skip_full:
        state["requested_scope"] = "ablations_only"
    if args.only:
        state["requested_scope"] = args.only
    for name, variant, rounds in (
        ("full", "configured", 2),
        ("no_hybrid", "no_hybrid", 2),
        ("no_grep", "no_grep", 2),
        ("single_round", "configured", 1),
    ):
        if args.only and name != args.only:
            continue
        if args.full_only and name != "full":
            continue
        if args.skip_full and name == "full":
            continue
        destination = out / name
        command = [
            sys.executable, "-m", "memory_dm.evaluation.locomo",
            "--env-file", str(args.env_file.resolve()),
            "--dataset", str(args.dataset.resolve()),
            "--variant", variant, "--with-derived",
            "--categories", "1", "2", "3", "4",
            "--workers", str(args.workers),
            "--question-workers", str(args.question_workers),
            "--out", str(destination),
        ]
        if args.reuse_ingestion_from:
            command.extend(["--reuse-ingestion-from", str(args.reuse_ingestion_from.resolve())])
        else:
            command.extend(["--shared-ingestion", str(out / "shared")])
        if (destination / "run_meta.json").exists():
            command.append("--resume")
        state["variants"][name] = {"status": "running", "rounds": rounds}
        manifest.write_text(json.dumps(state, indent=2) + "\n")
        print(f"Starting {name}", flush=True)
        with (out / f"{name}.log").open("a") as log:
            for attempt in range(3):
                result = subprocess.run(
                    command, cwd=root, env={**env, "RETRIEVAL_MAX_ROUNDS": str(rounds)},
                    stdout=log, stderr=subprocess.STDOUT, check=False,
                )
                if result.returncode == 0:
                    break
                if "--resume" not in command:
                    command.append("--resume")
                print(f"{name}: retrying incomplete questions (attempt {attempt + 1})", flush=True)
        state["variants"][name]["status"] = "completed" if result.returncode == 0 else "failed"
        state["variants"][name]["exit_code"] = result.returncode
        manifest.write_text(json.dumps(state, indent=2) + "\n")
        if result.returncode:
            print(f"Stopped: {name} failed; see {out / (name + '.log')}", flush=True)
            return result.returncode
        if args.judge:
            judge_command = [
                sys.executable, str(root / "scripts/judge_locomo.py"),
                "--input", str(destination / "items.jsonl"),
                "--out", str(destination / "judge"),
                "--env-file", str(args.env_file.resolve()), "--workers", "128",
            ]
            if args.prompt_source is not None:
                judge_command.extend(["--prompt-source", str(args.prompt_source.resolve())])
            with (out / f"{name}-judge.log").open("a") as log:
                for _ in range(3):
                    judged = subprocess.run(judge_command, cwd=root, stdout=log,
                                            stderr=subprocess.STDOUT, check=False)
                    if judged.returncode == 0:
                        break
            if judged.returncode:
                print(f"Judge failed for {name}; see judge log", flush=True)
                return judged.returncode
        rows = []
        for completed in state["variants"]:
            metrics_path = out / completed / "metrics.json"
            judge_path = out / completed / "judge/metrics.json"
            if metrics_path.exists():
                values = json.loads(metrics_path.read_text())
                rows.append({
                    "variant": completed, "f1": values["f1"],
                    "by_category": values["by_category"],
                    "retrieval_cost": values["retrieval_cost"],
                    "judge": json.loads(judge_path.read_text()) if judge_path.exists() else None,
                })
        (out / "comparison.json").write_text(json.dumps(rows, indent=2) + "\n")
    print("Requested variants completed; ready for post-hoc judging.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
