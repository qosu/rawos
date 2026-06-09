-- Migration 016: episodic memory + cooldown key fix
-- rawos persistent cross-session understanding and stable cooldown keys

CREATE TABLE IF NOT EXISTS episodic_memory (
    id               TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id          TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    trigger_type     TEXT    NOT NULL,
    domain           TEXT    NOT NULL,
    repo_root        TEXT,
    inferred_goal    TEXT    NOT NULL,
    decision         TEXT    NOT NULL CHECK (decision IN ('contribute','signal','silence')),
    action_summary   TEXT,
    outcome          TEXT    CHECK (outcome IN ('good','bad','unrated')) DEFAULT 'unrated',
    self_confidence  REAL,
    ts               INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_episodic_user_ts
    ON episodic_memory(user_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_episodic_user_domain
    ON episodic_memory(user_id, domain, ts DESC);

CREATE INDEX IF NOT EXISTS idx_episodic_repo
    ON episodic_memory(user_id, repo_root, ts DESC);

ALTER TABLE proactive_artifacts ADD COLUMN cooldown_key TEXT;
