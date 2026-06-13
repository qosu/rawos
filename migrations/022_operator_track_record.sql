-- Migration 022: operator track record (Milestone 3 — "The being as the machine's operator").
--
-- One row per (user, operation_class, target), e.g.
-- (rawos-entity, "file_edit", "/etc/caddy/Caddyfile").
-- A class graduates from propose-only to auto-apply-with-rollback only after
-- verified_successes reaches GRADUATION_THRESHOLD (kernel.track_record, currently 3).
-- Graduation state is advanced by db.update_operator_track_record, which reuses
-- kernel.track_record._advance_state verbatim (pure, class-agnostic function).
-- The existing autonomy_track_record (code-fix path) is completely separate.

PRAGMA user_version = 22;

CREATE TABLE IF NOT EXISTS operator_track_record (
    id                  TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id             TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    operation_class     TEXT    NOT NULL,
    target              TEXT    NOT NULL,
    verified_successes  INTEGER NOT NULL DEFAULT 0,
    graduated           INTEGER NOT NULL DEFAULT 0 CHECK (graduated IN (0, 1)),
    last_outcome        TEXT,
    last_target_sha     TEXT,
    pending_since       INTEGER,
    updated_at          INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE(user_id, operation_class, target)
);

CREATE INDEX IF NOT EXISTS idx_operator_track_record_lookup
    ON operator_track_record(user_id, operation_class, target);
