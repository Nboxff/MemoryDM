"""Post-hoc LoCoMo judging with LightMem's unchanged accuracy prompt."""

import argparse
import ast
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path
from statistics import mean

import httpx
from openai import OpenAI

from memory_dm.config import Settings
from memory_dm.evaluation.common import digest, write_json
from memory_dm.llm.providers import get_adapter


def load_prompt(path=None):
    if path is None:
        return files("memory_dm.prompts").joinpath("locomo_judge.md").read_text(encoding="utf-8")
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "ACCURACY_PROMPT" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise ValueError("LightMem ACCURACY_PROMPT not found")


def parse_label(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    label = json.loads(text)["label"]
    if label not in ("CORRECT", "WRONG"):
        raise ValueError("Judge label must be CORRECT or WRONG")
    return label


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--workers", type=int, default=128)
    parser.add_argument("--prompt-source", type=Path,
                        help="Optional alternative LightMem llm_judge.py; defaults to the bundled prompt")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("workers must be positive")
    prompt = load_prompt(args.prompt_source)
    records = [json.loads(line) for line in args.input.read_text().splitlines() if line.strip()]
    records = [r for r in records if int(r["category"]) != 5]
    if any(r["status"] != "completed" for r in records):
        raise ValueError("Complete failed QA before judging")
    config = Settings.from_env(args.env_file).llm
    args.out.mkdir(parents=True, exist_ok=True)
    safe_config = asdict(config)
    safe_config.pop("api_key")
    identity = {"input_sha256": digest(args.input), "prompt": prompt,
                "judge_configuration": safe_config}
    meta = args.out / "judge_meta.json"
    if meta.exists() and json.loads(meta.read_text()) != identity:
        raise ValueError("Judge input, prompt or configuration changed")
    write_json(meta, identity)
    (args.out / "prompt.txt").write_text(prompt)
    started = time.perf_counter()
    options = get_adapter(config.provider).adapt(config, {
        "response_format": {"type": "json_object"}, "temperature": 0.0,
    })
    with OpenAI(
        base_url=config.base_url, api_key=config.api_key, timeout=120, max_retries=2,
        http_client=httpx.Client(limits=httpx.Limits(
            max_connections=args.workers, max_keepalive_connections=args.workers)),
    ) as client:
        def judge(record):
            key = hashlib.sha256(json.dumps(
                [record["sample_id"], record["question_index"]]).encode()).hexdigest()
            path = args.out / "questions" / (key + ".json")
            if path.exists():
                saved = json.loads(path.read_text())
                if saved["status"] == "completed":
                    return saved
            result = {k: record[k] for k in (
                "sample_id", "question_index", "category", "question", "reference", "prediction")}
            for attempt in range(3):
                try:
                    response = client.chat.completions.create(
                        model=config.model, messages=[{"role": "user", "content": prompt.format(
                            question=record["question"], gold_answer=record["reference"],
                            generated_answer=record["prediction"])}], **options)
                    if response.choices[0].finish_reason != "stop":
                        raise ValueError("Incomplete judge response")
                    raw = response.choices[0].message.content or ""
                    label = parse_label(raw)
                    result.update(status="completed", label=label, score=int(label == "CORRECT"),
                                  response=raw, attempts=attempt + 1,
                                  usage=response.usage.model_dump() if response.usage else None)
                    break
                except Exception as error:
                    if attempt == 2:
                        result.update(status="error", error_type=type(error).__name__)
                    else:
                        time.sleep(2 ** attempt)
            write_json(path, result)
            return result

        results = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(judge, r) for r in records]
            for future in as_completed(futures):
                results.append(future.result())
                if len(results) % 100 == 0 or len(results) == len(records):
                    print(f"Judged {len(results)}/{len(records)}; "
                          f"errors={sum(r['status'] == 'error' for r in results)}", flush=True)
    valid = [r for r in results if r["status"] == "completed"]
    metrics = {
        "model": config.model, "workers": args.workers, "expected_questions": len(records),
        "completed_questions": len(valid), "failed_questions": len(results) - len(valid),
        "status": "completed" if len(valid) == len(records) else "partial",
        "llm_as_judge": mean(r["score"] for r in valid) if valid else None,
        "by_category": {}, "elapsed_seconds_this_invocation": time.perf_counter() - started,
    }
    for category in (1, 2, 3, 4):
        group = [r for r in valid if r["category"] == category]
        metrics["by_category"][str(category)] = {
            "count": len(group), "accuracy": mean(r["score"] for r in group) if group else None}
    write_json(args.out / "metrics.json", metrics)
    (args.out / "items.jsonl").write_text("".join(
        json.dumps(r, ensure_ascii=False) + "\n"
        for r in sorted(results, key=lambda r: (r["sample_id"], r["question_index"]))))
    print(json.dumps(metrics, indent=2), flush=True)
    return 0 if metrics["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
