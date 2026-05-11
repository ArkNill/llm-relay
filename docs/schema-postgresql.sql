-- llm-relay PostgreSQL schema
-- Status: DRAFT (do not execute without review)
-- Migrated from: SQLite usage.db (13 tables, 37 days, ~310K rows)
--
-- Key changes from SQLite:
--   ts REAL          → TIMESTAMPTZ
--   TEXT (JSON)      → JSONB
--   INTEGER (bool)   → BOOLEAN
--   AUTOINCREMENT    → SERIAL / BIGSERIAL
--   + pgvector for embeddings
--   + proper constraints and indexes

-- ══════════════════════════════════════════════
-- Extensions
-- ══════════════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS vector;    -- pgvector

-- ══════════════════════════════════════════════
-- 1. requests — API request/response usage log
-- ══════════════════════════════════════════════

CREATE TABLE requests (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id      TEXT,
    model           TEXT,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_creation  INTEGER NOT NULL DEFAULT 0,
    cache_read      INTEGER NOT NULL DEFAULT 0,
    read_ratio      REAL DEFAULT 0.0,
    status_code     SMALLINT,
    latency_ms      REAL,
    endpoint        TEXT,
    is_stream       BOOLEAN NOT NULL DEFAULT FALSE,
    raw_usage       JSONB,
    request_body_bytes  INTEGER DEFAULT 0,
    ratelimit_headers   JSONB,
    estimated_cost_usd  REAL DEFAULT 0.0,
    message_count       INTEGER DEFAULT 0,
    ephemeral_1h_tokens INTEGER DEFAULT 0,
    ephemeral_5m_tokens INTEGER DEFAULT 0
);

CREATE INDEX idx_requests_ts ON requests(ts);
CREATE INDEX idx_requests_session ON requests(session_id);
CREATE INDEX idx_requests_model ON requests(model);

-- ══════════════════════════════════════════════
-- 2. microcompact_events
-- ══════════════════════════════════════════════

CREATE TABLE microcompact_events (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id      TEXT,
    request_id      BIGINT REFERENCES requests(id),
    cleared_count   INTEGER DEFAULT 0,
    total_tool_results INTEGER DEFAULT 0,
    cleared_indices JSONB,
    message_count   INTEGER DEFAULT 0
);

CREATE INDEX idx_mc_ts ON microcompact_events(ts);

-- ══════════════════════════════════════════════
-- 3. budget_events
-- ══════════════════════════════════════════════

CREATE TABLE budget_events (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id      TEXT,
    msg_index       INTEGER,
    tool_name       TEXT,
    content_chars   INTEGER DEFAULT 0,
    truncated       BOOLEAN NOT NULL DEFAULT FALSE,
    marker          TEXT
);

CREATE INDEX idx_budget_ts ON budget_events(ts);

-- ══════════════════════════════════════════════
-- 4. prune_events
-- ══════════════════════════════════════════════

CREATE TABLE prune_events (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id      TEXT,
    tier            TEXT,
    messages_before INTEGER DEFAULT 0,
    messages_after  INTEGER DEFAULT 0,
    chars_before    INTEGER DEFAULT 0,
    chars_after     INTEGER DEFAULT 0,
    chars_removed   INTEGER DEFAULT 0,
    savings_pct     REAL DEFAULT 0.0,
    strategies      TEXT
);

-- ══════════════════════════════════════════════
-- 5. health_checks
-- ══════════════════════════════════════════════

CREATE TABLE health_checks (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    check_name      TEXT,
    status          TEXT,
    detail          TEXT,
    session_id      TEXT
);

-- ══════════════════════════════════════════════
-- 6. intercept_events
-- ══════════════════════════════════════════════

CREATE TABLE intercept_events (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id      TEXT,
    endpoint        TEXT,
    flags_count     INTEGER DEFAULT 0,
    flags_overridden TEXT
);

CREATE INDEX idx_intercept_ts ON intercept_events(ts);

-- ══════════════════════════════════════════════
-- 7. session_terminals
-- ══════════════════════════════════════════════

CREATE TABLE session_terminals (
    session_id      TEXT PRIMARY KEY,
    tty             TEXT,
    cc_pid          INTEGER,
    term_pid        INTEGER,
    term_name       TEXT,
    updated_ts      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ══════════════════════════════════════════════
-- 8. cache_diagnostics
-- ══════════════════════════════════════════════

CREATE TABLE cache_diagnostics (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id      TEXT,
    cc_version      TEXT,
    fingerprint     TEXT,
    system_block_count  INTEGER DEFAULT 0,
    system_preview  TEXT,
    msg0_block_count    INTEGER DEFAULT 0,
    msg0_preview    TEXT,
    drifted_blocks  TEXT,
    tools_count     INTEGER DEFAULT 0,
    tools_reordered BOOLEAN NOT NULL DEFAULT FALSE,
    ttl_injected    INTEGER DEFAULT 0
);

CREATE INDEX idx_cache_diag_ts ON cache_diagnostics(ts);
CREATE INDEX idx_cache_diag_session ON cache_diagnostics(session_id);

-- ══════════════════════════════════════════════
-- 9. conversation_turns — session history
-- ══════════════════════════════════════════════

CREATE TABLE conversation_turns (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id      TEXT NOT NULL,
    request_id      BIGINT,
    turn_number     INTEGER NOT NULL,
    storage_mode    TEXT NOT NULL DEFAULT 'delta',
    request_messages    JSONB,
    response_message    JSONB,
    thinking_blocks     JSONB,
    model           TEXT,
    temperature     REAL,
    max_tokens      INTEGER,
    total_message_count     INTEGER,
    previous_message_count  INTEGER,
    request_size_bytes  INTEGER DEFAULT 0,
    response_size_bytes INTEGER DEFAULT 0,
    provider        TEXT DEFAULT 'anthropic',
    -- pgvector: embedding for semantic search
    summary         TEXT,
    embedding       vector(1536)
);

CREATE INDEX idx_conv_turns_session ON conversation_turns(session_id, turn_number);
CREATE INDEX idx_conv_turns_ts ON conversation_turns(ts);

-- ══════════════════════════════════════════════
-- 10. compaction_events
-- ══════════════════════════════════════════════

CREATE TABLE compaction_events (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    session_id      TEXT NOT NULL,
    turn_number     INTEGER NOT NULL,
    previous_count  INTEGER,
    current_count   INTEGER,
    dropped_count   INTEGER,
    previous_tokens INTEGER,
    current_tokens  INTEGER,
    token_drop_pct  REAL,
    dropped_roles   TEXT
);

CREATE INDEX idx_compaction_session ON compaction_events(session_id);

-- ══════════════════════════════════════════════
-- 11. delegations — CLI orchestration log
-- ══════════════════════════════════════════════

CREATE TABLE delegations (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    cli_id          TEXT NOT NULL,
    auth_method     TEXT,
    prompt_hash     TEXT,
    prompt_preview  TEXT,
    model           TEXT,
    working_dir     TEXT,
    success         BOOLEAN NOT NULL DEFAULT FALSE,
    exit_code       INTEGER DEFAULT 0,
    duration_ms     REAL DEFAULT 0.0,
    output_chars    INTEGER DEFAULT 0,
    error           TEXT,
    strategy        TEXT
);

CREATE INDEX idx_deleg_ts ON delegations(ts);
CREATE INDEX idx_deleg_cli ON delegations(cli_id);

-- ══════════════════════════════════════════════
-- 12. knowledge_embeddings — pgvector knowledge search (NEW)
-- ══════════════════════════════════════════════

CREATE TABLE knowledge_embeddings (
    id              SERIAL PRIMARY KEY,
    file_path       TEXT NOT NULL,
    chunk_text      TEXT NOT NULL,
    embedding       vector(1536),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_knowledge_embedding ON knowledge_embeddings
    USING hnsw (embedding vector_cosine_ops);

-- ══════════════════════════════════════════════
-- 13. schema_version — migration tracking
-- ══════════════════════════════════════════════

CREATE TABLE schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    description TEXT
);

INSERT INTO schema_version (version, description)
VALUES (1, 'Initial PostgreSQL schema migrated from SQLite');
