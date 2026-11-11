"""Content fingerprints and atomic, credential-free benchmark bookkeeping."""

import hashlib
import json
import subprocess
from dataclasses import asdict
from pathlib import Path

from memory_dm.config import Settings


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def git(root: Path, *args) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def settings_metadata(settings: Settings) -> dict:
    value = asdict(settings)
    value.pop("db_path")
    value.pop("user_id")
    for key in ("llm", "embedding"):
        value[key].pop("api_key")
    # Normalize tuples before comparing a fresh configuration to a JSON manifest.
    return json.loads(json.dumps(value))
