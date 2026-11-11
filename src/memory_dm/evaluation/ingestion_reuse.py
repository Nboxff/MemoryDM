"""Explicit, verified reuse of imported LoCoMo memories across retrieval revisions."""

import json
from pathlib import Path

from memory_dm.evaluation.common import digest, settings_metadata


def reusable_ingestion(run: Path, samples, settings, dataset: Path, timezone: str) -> dict:
    run = run.resolve()
    manifest = run / "run_meta.json"
    meta = json.loads(manifest.read_text())
    config = meta["configuration"]
    if config["dataset_sha256"] != digest(dataset) or config["timezone"] != timezone:
        raise ValueError("Reusable ingestion dataset or timezone differs")
    original = dict(config["settings"])
    current = settings_metadata(settings)
    for key in ("retrieval_methods", "retrieval_max_rounds"):
        original.pop(key, None)
        current.pop(key, None)
    if original != current:
        raise ValueError("Reusable ingestion settings differ beyond methods and rounds")
    diagnostics = {s["sample_id"]: s for s in meta["samples"]}
    indexes = {sample_id: i for i, sample_id in enumerate(config["selection"])}
    result = {}
    for sample in samples:
        if sample.sample_id not in indexes or sample.sample_id not in diagnostics:
            raise ValueError("Reusable ingestion is missing a selected sample")
        imported = diagnostics[sample.sample_id].get("ingestion", {})
        snapshot = run / "states" / f"{indexes[sample.sample_id]:04d}" / "snapshot.sqlite3"
        if (
            imported.get("sample_id") != sample.sample_id
            or not snapshot.is_file()
            or digest(snapshot) != imported.get("snapshot_sha256")
        ):
            raise ValueError("Reusable ingestion snapshot is missing or changed")
        result[sample.sample_id] = {"snapshot": str(snapshot), "ingestion": imported}
    return {"run": str(run), "manifest_sha256": digest(manifest), "samples": result}
