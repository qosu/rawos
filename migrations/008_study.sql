-- watched_paths: arbitrary dirs the personal watcher monitors, mapped to user_id.
-- This is how the researcher's actual workdirs generate context_events.
CREATE TABLE IF NOT EXISTS watched_paths (
    id         TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id    TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    path       TEXT    NOT NULL,
    label      TEXT    NOT NULL DEFAULT '',  -- human label for this workspace
    active     INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_watched_paths_user_path ON watched_paths(user_id, path);

-- study_config: key/value store for study parameters
CREATE TABLE IF NOT EXISTS study_config (
    key        TEXT    PRIMARY KEY,
    value      TEXT    NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
);

-- study_daily_snapshots: aggregated daily metrics for research analysis
CREATE TABLE IF NOT EXISTS study_daily_snapshots (
    id                  TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    snapshot_date       TEXT    NOT NULL,   -- YYYY-MM-DD
    user_id             TEXT,
    total_inferences    INTEGER NOT NULL DEFAULT 0,
    total_artifacts     INTEGER NOT NULL DEFAULT 0,
    total_rated         INTEGER NOT NULL DEFAULT 0,
    precision_at_3      REAL,              -- rated>=3 / total_rated
    avg_rating          REAL,
    avg_confidence      REAL,
    avg_timeliness      REAL,
    source_breakdown    TEXT,              -- JSON: {rule:N, classifier:N, llm:N}
    domain_breakdown    TEXT,              -- JSON: {debugging:N, ...}
    timing_breakdown    TEXT,              -- JSON: timeliness score histogram
    snapshot_at         INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_study_snapshots_date_user ON study_daily_snapshots(snapshot_date, user_id);

-- Seed study configuration defaults
INSERT OR IGNORE INTO study_config (key, value) VALUES
    ('study_start_date',        '2026-06-08'),
    ('study_duration_days',     '30'),
    ('target_precision',        '0.65'),
    ('target_artifacts_per_day','3'),
    ('min_rated_for_h1',        '20'),
    ('participant',             'researcher');
