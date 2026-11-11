"""Migrate fragmented profiles to one document, without retaining source mappings."""

import sqlite3
from uuid import uuid4

from memory_dm.profile.tokens import shorten_legacy_profile


def install_profile_schema(connection: sqlite3.Connection) -> None:
    triggers = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name GLOB 'profile_*' "
        "AND tbl_name != 'user_profiles'"
    ).fetchall()
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        # Source edits must no longer cascade to an unlinked profile document.
        for (name,) in triggers:
            connection.execute('DROP TRIGGER IF EXISTS "' + name.replace('"', '""') + '"')
        connection.execute("""CREATE TABLE IF NOT EXISTS user_profiles (
            user_id TEXT PRIMARY KEY NOT NULL,
            profile_id TEXT UNIQUE NOT NULL,
            content TEXT NOT NULL CHECK(length(trim(content)) > 0),
            created_at TEXT NOT NULL
        )""")
        legacy = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='profile_entries'"
        ).fetchone()
        if legacy:
            users = connection.execute("SELECT DISTINCT user_id FROM profile_entries").fetchall()
            for (user_id,) in users:
                rows = connection.execute(
                    "SELECT section, content, created_at FROM profile_entries "
                    "WHERE user_id=? ORDER BY created_at DESC, entry_id",
                    (user_id,),
                ).fetchall()
                text = "# User profile\n\n" + "\n".join(f"- {r[0]}: {r[1]}" for r in rows)
                connection.execute(
                    "INSERT OR IGNORE INTO user_profiles VALUES (?, ?, ?, ?)",
                    (user_id, str(uuid4()), shorten_legacy_profile(text), rows[-1][2]),
                )
        for table in ("profile_sources", "profile_groups", "profile_entries", "profile_seen"):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.execute("""CREATE TABLE IF NOT EXISTS profile_progress (
            user_id TEXT PRIMARY KEY NOT NULL,
            input_hash TEXT NOT NULL, attempts INTEGER NOT NULL, status TEXT NOT NULL
        )""")
        if legacy:
            connection.execute("DELETE FROM profile_progress")
        for table, prefix in (("experience_memories", "experience"), ("user_profiles", "profile")):
            for operation in ("INSERT", "UPDATE", "DELETE"):
                row = "old" if operation == "DELETE" else "new"
                name = f"{prefix}_revision_{operation.lower()}"
                connection.execute(f"""CREATE TRIGGER IF NOT EXISTS {name}
                    AFTER {operation} ON {table} BEGIN
                        INSERT INTO retrieval_revisions(user_id, revision) VALUES ({row}.user_id, 1)
                        ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1;
                    END""")
