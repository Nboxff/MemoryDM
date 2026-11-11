CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY NOT NULL,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_turns (
    turn_id TEXT PRIMARY KEY NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant')),
    content TEXT NOT NULL CHECK (length(trim(content)) > 0),
    timestamp TEXT NOT NULL,
    UNIQUE (session_id, turn_id)
);

CREATE INDEX IF NOT EXISTS idx_turns_session ON conversation_turns(session_id);

CREATE TABLE IF NOT EXISTS atomic_memories (
    memory_id TEXT PRIMARY KEY NOT NULL,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    memory_type TEXT NOT NULL CHECK (
        memory_type IN ('PROFILE', 'PREFERENCE', 'INTENTION', 'INSTRUCTION', 'EPISODE')
    ),
    valid_start TEXT,
    valid_end TEXT,
    valid_point TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1)
);

CREATE TABLE IF NOT EXISTS memory_evidence (
    evidence_id INTEGER PRIMARY KEY,
    memory_id TEXT NOT NULL REFERENCES atomic_memories(memory_id),
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    span_start INTEGER,
    span_end INTEGER,
    FOREIGN KEY (session_id, turn_id) REFERENCES conversation_turns(session_id, turn_id),
    CHECK (
        (span_start IS NULL AND span_end IS NULL)
        OR (span_start IS NOT NULL AND span_end IS NOT NULL
            AND span_start >= 0 AND span_end > span_start)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_reference
ON memory_evidence(memory_id, turn_id, COALESCE(span_start, -1), COALESCE(span_end, -1));

CREATE INDEX IF NOT EXISTS idx_memories_user_status ON atomic_memories(user_id, status);

CREATE VIRTUAL TABLE IF NOT EXISTS atomic_memories_fts USING fts5(
    memory_id UNINDEXED, content, tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS fts_pending (
    memory_id TEXT PRIMARY KEY NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_vector_ids (
    vector_id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL UNIQUE REFERENCES atomic_memories(memory_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memory_embeddings (
    vector_id INTEGER NOT NULL REFERENCES memory_vector_ids(vector_id) ON DELETE CASCADE,
    embedding_key TEXT NOT NULL,
    content TEXT NOT NULL,
    dimension INTEGER NOT NULL CHECK (dimension > 0),
    vector BLOB NOT NULL,
    PRIMARY KEY (vector_id, embedding_key),
    CHECK (length(vector) = dimension * 4)
);

CREATE TABLE IF NOT EXISTS retrieval_revisions (
    user_id TEXT PRIMARY KEY NOT NULL,
    revision INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS dense_snapshots (
    user_id TEXT NOT NULL,
    embedding_key TEXT NOT NULL,
    revision INTEGER NOT NULL,
    index_data BLOB NOT NULL,
    PRIMARY KEY (user_id, embedding_key)
);

CREATE TRIGGER IF NOT EXISTS atomic_search_insert AFTER INSERT ON atomic_memories BEGIN
    INSERT OR IGNORE INTO fts_pending(memory_id) VALUES (new.memory_id);
    INSERT INTO retrieval_revisions(user_id, revision) VALUES (new.user_id, 1)
        ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1;
    DELETE FROM dense_snapshots WHERE user_id = new.user_id;
END;

CREATE VIRTUAL TABLE IF NOT EXISTS conversation_turns_fts USING fts5(
    turn_id UNINDEXED, content, tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS raw_fts_pending (
    turn_id TEXT PRIMARY KEY NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_revisions (
    user_id TEXT PRIMARY KEY NOT NULL,
    revision INTEGER NOT NULL
);

CREATE TRIGGER IF NOT EXISTS raw_search_insert AFTER INSERT ON conversation_turns BEGIN
    INSERT OR IGNORE INTO raw_fts_pending(turn_id) VALUES (new.turn_id);
    INSERT INTO conversation_revisions(user_id, revision)
        SELECT user_id, 1 FROM sessions WHERE session_id = new.session_id
        ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1;
END;

CREATE TRIGGER IF NOT EXISTS raw_search_delete AFTER DELETE ON conversation_turns BEGIN
    INSERT OR IGNORE INTO raw_fts_pending(turn_id) VALUES (old.turn_id);
    INSERT INTO conversation_revisions(user_id, revision)
        SELECT user_id, 1 FROM sessions WHERE session_id = old.session_id
        ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1;
END;

CREATE TRIGGER IF NOT EXISTS raw_search_update AFTER UPDATE ON conversation_turns BEGIN
    INSERT OR IGNORE INTO raw_fts_pending(turn_id) VALUES (old.turn_id);
    INSERT OR IGNORE INTO raw_fts_pending(turn_id) VALUES (new.turn_id);
    INSERT INTO conversation_revisions(user_id, revision)
        SELECT user_id, 1 FROM sessions WHERE session_id = old.session_id
        ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1;
    INSERT INTO conversation_revisions(user_id, revision)
        SELECT user_id, 1 FROM sessions WHERE session_id = new.session_id
          AND new.session_id != old.session_id
        ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1;
END;

CREATE TRIGGER IF NOT EXISTS atomic_search_update AFTER UPDATE ON atomic_memories BEGIN
    INSERT OR IGNORE INTO fts_pending(memory_id) VALUES (old.memory_id);
    INSERT OR IGNORE INTO fts_pending(memory_id) VALUES (new.memory_id);
    INSERT INTO retrieval_revisions(user_id, revision) VALUES (old.user_id, 1)
        ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1;
    INSERT INTO retrieval_revisions(user_id, revision)
        SELECT new.user_id, 1 WHERE new.user_id != old.user_id
        ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1;
    DELETE FROM dense_snapshots WHERE user_id IN (old.user_id, new.user_id);
END;

CREATE TRIGGER IF NOT EXISTS atomic_search_delete AFTER DELETE ON atomic_memories BEGIN
    INSERT OR IGNORE INTO fts_pending(memory_id) VALUES (old.memory_id);
    INSERT INTO retrieval_revisions(user_id, revision) VALUES (old.user_id, 1)
        ON CONFLICT(user_id) DO UPDATE SET revision = revision + 1;
    DELETE FROM dense_snapshots WHERE user_id = old.user_id;
END;

CREATE TRIGGER IF NOT EXISTS embeddings_search_update AFTER UPDATE ON memory_embeddings BEGIN
    DELETE FROM dense_snapshots WHERE user_id IN (
        SELECT m.user_id FROM atomic_memories AS m JOIN memory_vector_ids AS v
            ON v.memory_id = m.memory_id WHERE v.vector_id IN (old.vector_id, new.vector_id)
    );
END;

CREATE TRIGGER IF NOT EXISTS embeddings_search_delete AFTER DELETE ON memory_embeddings BEGIN
    DELETE FROM dense_snapshots WHERE user_id IN (
        SELECT m.user_id FROM atomic_memories AS m JOIN memory_vector_ids AS v
            ON v.memory_id = m.memory_id WHERE v.vector_id = old.vector_id
    );
END;

CREATE TABLE IF NOT EXISTS memory_links (
    source_id TEXT NOT NULL REFERENCES atomic_memories(memory_id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES atomic_memories(memory_id) ON DELETE CASCADE,
    operation TEXT NOT NULL CHECK (operation IN ('MERGE', 'CORRECT', 'SUPERSEDE', 'KEEP_BOTH')),
    PRIMARY KEY (source_id, target_id, operation),
    CHECK (source_id != target_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_links_target ON memory_links(target_id);

CREATE TABLE IF NOT EXISTS memory_processed_turns (
    turn_id TEXT PRIMARY KEY NOT NULL REFERENCES conversation_turns(turn_id) ON DELETE CASCADE
);

-- Source text remains in conversation_turns. Recovery never stores a replay copy.
CREATE TABLE IF NOT EXISTS memory_maintenance_failures (
    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
    input_hash TEXT NOT NULL,
    attempts INTEGER NOT NULL CHECK (attempts >= 0),
    diagnostics TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS conversation_memory_changed
AFTER UPDATE ON conversation_turns BEGIN
    DELETE FROM memory_processed_turns WHERE turn_id IN (old.turn_id, new.turn_id);
END;
