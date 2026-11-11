"""Read-only target-literal screening of saved pre-probe Bemula snapshots.

This is candidate generation, NOT a semantic forgetting score. No-hit is unknown.
Uses all complete gold values without a minimum word count, casefolded with
whitespace collapsed; no synonyms, paraphrases, or inference are scored.
"""

import argparse
import hashlib
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path


def read_jsonl(path):
    return [json.loads(line) for line in path.open() if line.strip()]


def normalize(value):
    return re.sub(r"\s+", " ", value.casefold()).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scan(records, facts):
    hits = []
    for fact in facts:
        value = normalize(fact["value"])
        if not value:
            raise ValueError(f"Empty target value: {fact['fact_id']}")
        for record in records:
            if value in normalize(record["content"]):
                hits.append({"fact_id": fact["fact_id"], "gold_value": fact["value"], **record})
    return hits


def snapshot(path):
    records = []
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        for layer, table, key in (
            ("RAW", "conversation_turns", "turn_id"),
            ("ATOMIC", "atomic_memories", "memory_id"),
            ("EXPERIENCE", "experience_memories", "experience_id"),
            ("PROFILE", "user_profiles", "profile_id"),
            ("EMBEDDING_TEXT", "memory_embeddings", "vector_id"),
        ):
            for row in db.execute(f"SELECT * FROM {table}"):
                record = {"layer": layer, "id": row[key], "content": row["content"]}
                for field in ("status", "timestamp", "role", "embedding_key"):
                    if field in row.keys():
                        record[field] = row[field]
                records.append(record)
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, action="append", required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    dialogues_path = args.dataset / "dialogues.jsonl"
    dialogues_hash = digest(dialogues_path)
    dialogues = {r["instance_id"]: r for r in read_jsonl(dialogues_path)}
    profiles = {r["profile_id"]: r for r in read_jsonl(args.dataset / "profiles.jsonl")}
    args.out.mkdir(parents=True, exist_ok=False)
    summaries, requests, witnesses = [], [], []
    for run in args.run:
        meta = json.loads((run / "run_meta.json").read_text())
        expected = [
            v for k, v in meta["configuration"]["inputs"].items() if k.endswith("/dialogues.jsonl")
        ]
        if expected != [dialogues_hash]:
            raise ValueError(f"Dialogue fingerprint mismatch: {run}")
        metrics = json.loads((run / "metrics.json").read_text())
        counts, final_layers, control_layers, conditional, eligible = (Counter() for _ in range(5))
        sources = []
        for directory in sorted((run / "states").iterdir()):
            events = read_jsonl(directory / "events.jsonl")
            instance_id = events[0]["instance_id"]
            dialogue = dialogues[instance_id]
            facts = {f["fact_id"]: f for f in profiles[dialogue["profile_id"]]["facts"]}
            stores = {}
            for phase in ("control", "forget"):
                path = directory / f"{phase}.snapshot.sqlite3"
                stores[phase] = snapshot(path)
                sources.append({"path": str(path), "sha256": digest(path)})
            for request in dialogue["forget_requests"]:
                targets = [facts[i] for i in request["target_fact_ids"]]
                before = scan(stores["control"], targets)
                after = scan(stores["forget"], targets)
                old_layers = {h["layer"] for h in before}
                new_layers = {h["layer"] for h in after}
                counts["requests"] += 1
                counts["requests_with_screenable_value"] += bool(targets)
                counts["requests_with_final_canonical_hit"] += bool(new_layers - {"EMBEDDING_TEXT"})
                counts["requests_with_control_canonical_hit"] += bool(
                    old_layers - {"EMBEDDING_TEXT"}
                )
                final_layers.update(new_layers)
                control_layers.update(old_layers)
                conditional.update(new_layers & old_layers)
                eligible.update(old_layers)
                counts["F6_requests_with_final_hit"] += request["forget_type"] == "F6" and bool(
                    new_layers
                )
                m = metrics["by_request"][request["request_id"]]
                counts["FE_one_with_final_hit"] += m["FE"] == 1 and bool(new_layers)
                item = {
                    "run": run.name,
                    "instance_id": instance_id,
                    **request,
                    "target_facts": targets,
                    "official_FE": m["FE"],
                    "official_strict_pass": m["strict_pass"],
                    "control_hit_layers": sorted(old_layers),
                    "final_hit_layers": sorted(new_layers),
                    "control_hits": before,
                    "final_hits": after,
                }
                if request["forget_type"] == "F4":
                    premise_ids = sorted({j for f in targets for j in f["source_facts"]})
                    item["F4_premise_candidates"] = scan(
                        stores["forget"], [facts[j] for j in premise_ids]
                    )
                requests.append(item)
                canonical = [h for h in after if h["layer"] != "EMBEDDING_TEXT"]
                if canonical:
                    order = {"ATOMIC": 0, "EXPERIENCE": 1, "RAW": 2, "PROFILE": 3}
                    witness = min(
                        canonical,
                        key=lambda h: (
                            order[h["layer"]],
                            len(h["content"]),
                            h["fact_id"],
                            str(h["id"]),
                        ),
                    )
                    witnesses.append(
                        {
                            "run": run.name,
                            "request_id": request["request_id"],
                            "forget_type": request["forget_type"],
                            "request_text": request["request_text"],
                            "official_FE": m["FE"],
                            "witness": witness,
                            "adjudication": "UNREVIEWED",
                        }
                    )
        summary = {
            "run": str(run),
            "counts": dict(counts),
            "final_hit_requests_by_layer": dict(final_layers),
            "control_hit_requests_by_layer": dict(control_layers),
            "final_and_control_hit_requests_by_layer": dict(conditional),
            "control_screening_denominators_by_layer": dict(eligible),
            "snapshots": sources,
        }
        summaries.append(summary)
        print(json.dumps({k: v for k, v in summary.items() if k != "snapshots"}, indent=2))
    result = {
        "matching_rule": "Complete normalized gold values, without a minimum word count",
        "scope": "Literal candidates only; no-hit is unknown. Final is before probes and "
        "relearning, after all dialogue. Control is an independent branch, "
        "not the immediate pre-request state. F6 needs separate adjudication.",
        "dialogues_sha256": dialogues_hash,
        "profiles_sha256": digest(args.dataset / "profiles.jsonl"),
        "runs": summaries,
        "requests": requests,
    }
    (args.out / "screening.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    (args.out / "candidate-witnesses.json").write_text(
        json.dumps(witnesses, ensure_ascii=False, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
