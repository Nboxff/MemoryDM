"""SQLite snapshots used by the paired Bemula evaluation."""

import sqlite3
from pathlib import Path


def snapshot(connection, destination: Path):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("Snapshot destination already exists")
    with sqlite3.connect(destination) as target:
        connection.backup(target)


def fork(source: Path, destination: Path):
    source = Path(source).resolve()
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as connection:
        snapshot(connection, destination)
