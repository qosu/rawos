-- rawos initial schema
-- All tables enforce user_id isolation at the column level.
-- FK enforcement: PRAGMA foreign_keys = ON must be set per connection.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Users
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id                  TEXT PRIMARY KEY,
    email               TEXT UNIQUE NOT NULL,
    password_hash       TEXT NOT NULL,
    tier                TEXT NOT NULL DEFAULT 'free',
    token_budget_daily  INTEGER NOT NULL DEFAULT 100000,
    tokens_used_today   INTEGER NOT NULL DEFAULT 0,
    budget_reset_date   TEXT NOT NULL DEFAULT '',  -- ISO date of last reset
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

-- ---------------------------------------------------------------------------
-- Projects (workspace — replaces "workdir")
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    workdir     TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id);

-- ---------------------------------------------------------------------------
-- Agents
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    parent_id   TEXT REFERENCES agents(id),
    status      TEXT NOT NULL DEFAULT 'dormant',
    goal        TEXT NOT NULL,
    model       TEXT NOT NULL DEFAULT 'deepseek-chat',
    token_used  INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agents_user    ON agents(user_id);
CREATE INDEX IF NOT EXISTS idx_agents_project ON agents(project_id, status);

-- ---------------------------------------------------------------------------
-- Intents
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS intents (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id          TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    agent_id            TEXT REFERENCES agents(id),
    raw_text            TEXT NOT NULL,
    goal                TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'pending',
    result_artifact_id  TEXT,   -- FK to artifacts; no REFERENCES to avoid circular dep
    created_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_intents_user    ON intents(user_id);
CREATE INDEX IF NOT EXISTS idx_intents_project ON intents(project_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Memories (episodic + semantic + procedural tiers — working tier is Redis)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id  TEXT REFERENCES projects(id) ON DELETE CASCADE,
    agent_id    TEXT REFERENCES agents(id),
    tier        TEXT NOT NULL,   -- episodic / semantic / procedural
    role        TEXT NOT NULL,   -- user / assistant / system / tool_result
    content     TEXT NOT NULL,   -- JSON: str or list[dict]
    embedding   BLOB,            -- sentence-transformer vector bytes
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER          -- NULL = permanent
);

CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project_id, tier, created_at);
CREATE INDEX IF NOT EXISTS idx_memories_agent   ON memories(agent_id, created_at);
CREATE INDEX IF NOT EXISTS idx_memories_expires ON memories(expires_at) WHERE expires_at IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Artifacts (outputs: files, websites, charts, documents, code)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    agent_id    TEXT REFERENCES agents(id),
    intent_id   TEXT REFERENCES intents(id),
    type        TEXT NOT NULL,   -- file / website / chart / document / code
    name        TEXT NOT NULL,
    path        TEXT,            -- absolute fs path
    content     TEXT,            -- inline if small
    mime_type   TEXT,
    size_bytes  INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Tools (registry — seeded once; rows are not user-owned)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tools (
    id            TEXT PRIMARY KEY,
    name          TEXT UNIQUE NOT NULL,
    description   TEXT NOT NULL,
    input_schema  TEXT NOT NULL,   -- JSON Schema
    sandbox_level TEXT NOT NULL DEFAULT 'read',
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    INTEGER NOT NULL
);

-- ---------------------------------------------------------------------------
-- Events (immutable audit log — INSERT only, never UPDATE/DELETE)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id  TEXT REFERENCES projects(id),
    agent_id    TEXT REFERENCES agents(id),
    type        TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',  -- JSON
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_user    ON events(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Refresh tokens (auth — separate from primitives)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS refresh_tokens (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT UNIQUE NOT NULL,
    expires_at  INTEGER NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_refresh_user ON refresh_tokens(user_id);

-- Phase 3 performance indexes
CREATE INDEX IF NOT EXISTS idx_memories_project_tier_time ON memories(project_id, tier, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_memories_project_time     ON memories(project_id, created_at DESC);
