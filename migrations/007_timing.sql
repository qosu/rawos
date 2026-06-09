-- timing_state: per-user state needed for incremental timing computation
-- Tracks domain transitions and session boundaries across scheduler ticks.
CREATE TABLE IF NOT EXISTS timing_state (
    user_id           TEXT    PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    last_domain       TEXT,           -- domain active at last check
    domain_changed_at INTEGER,        -- unixepoch when domain last changed
    session_start_at  INTEGER,        -- unixepoch of last session start detection
    updated_at        INTEGER NOT NULL DEFAULT (unixepoch())
);

-- Add timing columns to inference_log so every inference captures its timing context.
-- These are nullable — pre-Phase-10 rows remain valid.
ALTER TABLE inference_log ADD COLUMN timeliness_score REAL;
ALTER TABLE inference_log ADD COLUMN timing_signals   TEXT;   -- JSON blob
