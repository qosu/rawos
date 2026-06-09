-- Migration 009: Rich perception context + trust engine foundation
-- Phase 1 upgrade: collector emits semantic git+session signals
-- Trust engine: autonomy_grants tracks earned levels per action type

PRAGMA user_version = 9;

-- Enrich context_events with git diff and session pattern data
ALTER TABLE context_events ADD COLUMN diff_summary       TEXT;
ALTER TABLE context_events ADD COLUMN diff_hunk          TEXT;
ALTER TABLE context_events ADD COLUMN session_edit_count INTEGER NOT NULL DEFAULT 1;
ALTER TABLE context_events ADD COLUMN stuck_signal       INTEGER NOT NULL DEFAULT 0;

-- Work sessions: track bounded activity windows for study metrics
CREATE TABLE IF NOT EXISTS work_sessions (
    id             TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id        TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id     TEXT    REFERENCES projects(id) ON DELETE SET NULL,
    started_at     INTEGER NOT NULL DEFAULT (unixepoch()),
    ended_at       INTEGER,
    files_edited   INTEGER NOT NULL DEFAULT 0,
    commits_made   INTEGER NOT NULL DEFAULT 0,
    stuck_signals  INTEGER NOT NULL DEFAULT 0,
    rawos_actions  INTEGER NOT NULL DEFAULT 0,
    good_actions   INTEGER NOT NULL DEFAULT 0
);

-- Autonomy grants: per-user per-action-type trust levels
-- Unlocked by track record, not by config
CREATE TABLE IF NOT EXISTS autonomy_grants (
    user_id      TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action_type  TEXT    NOT NULL,   -- analysis|code_suggestion|test|commit|execute
    level        INTEGER NOT NULL DEFAULT 0,
    granted_at   INTEGER,
    good_count   INTEGER NOT NULL DEFAULT 0,
    bad_count    INTEGER NOT NULL DEFAULT 0,
    last_action  INTEGER,
    PRIMARY KEY (user_id, action_type)
);

-- Seed default analysis grant for all existing users
INSERT OR IGNORE INTO autonomy_grants (user_id, action_type, level, granted_at)
SELECT id, 'analysis', 0, unixepoch() FROM users;
