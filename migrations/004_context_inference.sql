-- Context events: raw behavioral observations per user
CREATE TABLE IF NOT EXISTS context_events (
    id          TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_type  TEXT    NOT NULL,   -- file_read, file_write, file_delete, intent_sent, artifact_created, command_run
    path        TEXT,               -- file path or resource identifier
    metadata    TEXT    NOT NULL DEFAULT '{}',  -- JSON: size, extension, project_id, command text, etc.
    ts          INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_context_events_user_ts ON context_events(user_id, ts DESC);

-- User model: aggregated semantic state per user (one row per user, upserted continuously)
CREATE TABLE IF NOT EXISTS user_model (
    user_id             TEXT    PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    current_project_id  TEXT    REFERENCES projects(id) ON DELETE SET NULL,
    inferred_stack      TEXT    NOT NULL DEFAULT '[]',   -- JSON array: ["python","fastapi","sqlite"]
    active_domains      TEXT    NOT NULL DEFAULT '[]',   -- JSON array: ["backend","auth","debugging"]
    recent_activity     TEXT    NOT NULL DEFAULT '[]',   -- JSON array of last 20 semantic events
    inferred_goal       TEXT,                            -- current inferred goal text
    goal_confidence     REAL    NOT NULL DEFAULT 0.0,
    goal_domain         TEXT,                            -- e.g. "debugging", "feature", "research"
    goal_updated_at     INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at          INTEGER NOT NULL DEFAULT (unixepoch())
);

-- Proactive artifacts: tracks every file rawos created without being asked
CREATE TABLE IF NOT EXISTS proactive_artifacts (
    id          TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    goal        TEXT    NOT NULL,           -- the inferred goal that triggered this
    confidence  REAL    NOT NULL,
    file_path   TEXT    NOT NULL,           -- absolute path on disk
    artifact_id TEXT    REFERENCES artifacts(id) ON DELETE SET NULL,
    agent_id    TEXT    REFERENCES agents(id) ON DELETE SET NULL,
    created_at  INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_proactive_artifacts_user ON proactive_artifacts(user_id, created_at DESC);
