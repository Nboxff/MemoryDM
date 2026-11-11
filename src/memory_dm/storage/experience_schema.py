"""Install experience storage and transaction-local source invalidation triggers."""

import sqlite3


def _upgrade_kinds(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='experience_memories'"
    ).fetchone()
    if row is None or "'WORKFLOW'" in row[0]:
        return
    # SQLite cannot alter a CHECK constraint. Rebuild only this table, retaining
    # every child row, index and trigger, with foreign-key checks before commit.
    triggers = connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
    ).fetchall()
    indexes = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND tbl_name='experience_memories' AND sql IS NOT NULL"
    ).fetchall()
    sql = (
        row[0]
        .replace("experience_memories", "experience_upgrade", 1)
        .replace(
            "('HABIT', 'PREFERENCE')",
            "('HABIT', 'PREFERENCE', 'WORKFLOW', 'SKILL', 'TOOL_STRATEGY')",
        )
    )
    if "'WORKFLOW'" not in sql:
        raise ValueError("Unrecognized experience schema; cannot safely migrate kind constraint")
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            for name, _ in triggers:
                connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
            connection.execute(sql)
            connection.execute("INSERT INTO experience_upgrade SELECT * FROM experience_memories")
            connection.execute("DROP TABLE experience_memories")
            connection.execute("ALTER TABLE experience_upgrade RENAME TO experience_memories")
            for (index,) in indexes:
                connection.execute(index)
            for _, trigger in triggers:
                connection.execute(trigger)
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Experience migration broke source references")
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _invalidate(parents: str) -> str:
    affected = f"SELECT group_id FROM experience_sources WHERE memory_id IN ({parents})"
    return f"""
        INSERT INTO experience_pending(user_id, memory_id, revision)
        SELECT DISTINCT m.user_id, m.memory_id, 1 FROM atomic_memories m
        WHERE m.memory_id IN ({parents}) OR m.memory_id IN (
            SELECT memory_id FROM experience_sources WHERE group_id IN ({affected}))
        ON CONFLICT(user_id, memory_id) DO UPDATE SET revision = revision + 1;
        DELETE FROM experience_groups WHERE group_id IN ({affected});
        DELETE FROM experience_memories
        WHERE user_id IN (SELECT user_id FROM atomic_memories WHERE memory_id IN ({parents}))
          AND NOT EXISTS (SELECT 1 FROM experience_groups g
                          WHERE g.experience_id = experience_memories.experience_id);
    """


def install_experience_schema(connection: sqlite3.Connection) -> None:
    _upgrade_kinds(connection)
    existed = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'experience_memories'"
    ).fetchone()
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS experience_memories (
            experience_id TEXT PRIMARY KEY NOT NULL,
            user_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN (
                'HABIT', 'PREFERENCE', 'WORKFLOW', 'SKILL', 'TOOL_STRATEGY')),
            content TEXT NOT NULL CHECK (length(trim(content)) > 0),
            valid_start TEXT, valid_end TEXT, valid_point TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_experience_owner ON experience_memories(user_id);
        CREATE TABLE IF NOT EXISTS experience_groups (
            group_id TEXT PRIMARY KEY NOT NULL,
            experience_id TEXT NOT NULL REFERENCES experience_memories(experience_id)
                ON DELETE CASCADE,
            basis TEXT NOT NULL CHECK (basis IN ('EXPLICIT', 'OBSERVED'))
        );
        CREATE INDEX IF NOT EXISTS idx_experience_group ON experience_groups(experience_id);
        CREATE TABLE IF NOT EXISTS experience_sources (
            group_id TEXT NOT NULL REFERENCES experience_groups(group_id) ON DELETE CASCADE,
            memory_id TEXT NOT NULL REFERENCES atomic_memories(memory_id) ON DELETE CASCADE,
            fingerprint TEXT NOT NULL,
            PRIMARY KEY (group_id, memory_id)
        );
        CREATE INDEX IF NOT EXISTS idx_experience_parent ON experience_sources(memory_id);
        CREATE TABLE IF NOT EXISTS experience_pending (
            user_id TEXT NOT NULL, memory_id TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (user_id, memory_id)
        );
        CREATE TABLE IF NOT EXISTS experience_failures (
            user_id TEXT PRIMARY KEY NOT NULL,
            input_hash TEXT NOT NULL, attempts INTEGER NOT NULL,
            error_code TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS experience_session_changes (
            session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
            memory_id TEXT NOT NULL REFERENCES atomic_memories(memory_id) ON DELETE CASCADE,
            PRIMARY KEY(session_id, memory_id)
        );
        CREATE TABLE IF NOT EXISTS experience_turn_epochs (
            epoch INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_id TEXT UNIQUE NOT NULL REFERENCES conversation_turns(turn_id) ON DELETE CASCADE,
            user_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS experience_revocations (
            revocation_id TEXT PRIMARY KEY NOT NULL,
            user_id TEXT NOT NULL, cutoff_epoch INTEGER NOT NULL
        );
        -- Revocations retain identifiers only, never deleted claims or replay text.
        CREATE TABLE IF NOT EXISTS experience_revoked_sources (
            revocation_id TEXT NOT NULL REFERENCES experience_revocations(revocation_id)
                ON DELETE CASCADE,
            turn_id TEXT NOT NULL,
            PRIMARY KEY (revocation_id, turn_id)
        );
        CREATE TRIGGER IF NOT EXISTS experience_turn_arrived
        AFTER INSERT ON conversation_turns BEGIN
            INSERT INTO experience_turn_epochs(turn_id, user_id)
            SELECT new.turn_id, user_id FROM sessions WHERE session_id = new.session_id;
        END;
        CREATE TRIGGER IF NOT EXISTS experience_atomic_arrived
        AFTER INSERT ON atomic_memories BEGIN
            INSERT INTO experience_pending VALUES (new.user_id, new.memory_id, 1)
            ON CONFLICT(user_id, memory_id) DO UPDATE SET revision = revision + 1;
        END;
    """)
    triggers = [
        ("atomic_update", "BEFORE UPDATE ON atomic_memories", "SELECT old.memory_id"),
        ("atomic_delete", "BEFORE DELETE ON atomic_memories", "SELECT old.memory_id"),
        ("evidence_insert", "AFTER INSERT ON memory_evidence", "SELECT new.memory_id"),
        ("evidence_delete", "BEFORE DELETE ON memory_evidence", "SELECT old.memory_id"),
        (
            "evidence_update",
            "BEFORE UPDATE ON memory_evidence",
            "SELECT old.memory_id UNION SELECT new.memory_id",
        ),
        (
            "raw_update",
            "BEFORE UPDATE ON conversation_turns",
            "SELECT memory_id FROM memory_evidence WHERE turn_id = old.turn_id",
        ),
        (
            "raw_delete",
            "BEFORE DELETE ON conversation_turns",
            "SELECT memory_id FROM memory_evidence WHERE turn_id = old.turn_id",
        ),
    ]
    for name, event, parents in triggers:
        connection.executescript(
            f"CREATE TRIGGER IF NOT EXISTS experience_{name} {event} BEGIN "
            f"{_invalidate(parents)} END;"
        )
    if not existed:
        with connection:
            connection.execute(
                """INSERT INTO experience_turn_epochs(turn_id, user_id)
                   SELECT t.turn_id, s.user_id FROM conversation_turns t
                   JOIN sessions s ON s.session_id = t.session_id ORDER BY t.rowid"""
            )
            connection.execute(
                """INSERT OR IGNORE INTO experience_pending
                   SELECT user_id, memory_id, 1 FROM atomic_memories"""
            )
