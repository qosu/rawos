-- rawos PostgreSQL schema — Phase 5
-- Equivalent to 001_initial.sql + 002_phase5.sql but in PostgreSQL syntax

CREATE TABLE IF NOT EXISTS users (
    id                  TEXT PRIMARY KEY,
    email               TEXT NOT NULL UNIQUE,
    password_hash       TEXT NOT NULL,
    tier                TEXT NOT NULL DEFAULT 'free',
    token_budget_daily  INTEGER NOT NULL DEFAULT 100000,
    tokens_used_today   INTEGER NOT NULL DEFAULT 0,
    budget_reset_date   TEXT NOT NULL DEFAULT '',
    is_admin            INTEGER NOT NULL DEFAULT 0,
    created_at          BIGINT NOT NULL,
    updated_at          BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    workdir     TEXT NOT NULL DEFAULT '',
    created_at  BIGINT NOT NULL,
    updated_at  BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    parent_id   TEXT REFERENCES agents(id),
    status      TEXT NOT NULL DEFAULT 'dormant',
    goal        TEXT NOT NULL,
    model       TEXT NOT NULL DEFAULT 'deepseek-chat',
    token_used  INTEGER NOT NULL DEFAULT 0,
    created_at  BIGINT NOT NULL,
    updated_at  BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS intents (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    agent_id            TEXT REFERENCES agents(id),
    raw_text            TEXT NOT NULL,
    goal                TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'pending',
    result_artifact_id  TEXT,
    created_at          BIGINT NOT NULL,
    updated_at          BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id  TEXT REFERENCES projects(id) ON DELETE CASCADE,
    agent_id    TEXT REFERENCES agents(id),
    tier        TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    embedding   BYTEA,
    created_at  BIGINT NOT NULL,
    expires_at  BIGINT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    agent_id    TEXT REFERENCES agents(id),
    intent_id   TEXT REFERENCES intents(id),
    type        TEXT NOT NULL DEFAULT 'file',
    name        TEXT NOT NULL,
    path        TEXT,
    content     TEXT,
    mime_type   TEXT,
    size_bytes  INTEGER NOT NULL DEFAULT 0,
    created_at  BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id  TEXT REFERENCES projects(id) ON DELETE CASCADE,
    agent_id    TEXT REFERENCES agents(id),
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    created_at  BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  BIGINT NOT NULL,
    created_at  BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS billing_events (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    intent_id   TEXT,
    tokens      INTEGER NOT NULL DEFAULT 0,
    model       TEXT NOT NULL DEFAULT '',
    event_type  TEXT NOT NULL DEFAULT 'intent',
    created_at  BIGINT NOT NULL
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_projects_user       ON projects(user_id);
CREATE INDEX IF NOT EXISTS idx_agents_user         ON agents(user_id);
CREATE INDEX IF NOT EXISTS idx_agents_project      ON agents(project_id, status);
CREATE INDEX IF NOT EXISTS idx_intents_project     ON intents(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_project    ON memories(project_id, tier, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_artifacts_project   ON artifacts(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_user_time    ON events(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_billing_user_time   ON billing_events(user_id, created_at DESC);
