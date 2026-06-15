-- Migration 028: owned_resource_history table (M3 — Owned-Resource Operator).
--
-- Audit ledger for every owned-resource operation outcome. One row per
-- operate_on_owned_resource() or execute_approved_owned_op() call that
-- produces a concrete outcome (applied or proposed — refused/errors are
-- logged but not ledgered unless they represent a decision, e.g. proposed).
--
-- op_type:        'workspace_gc' | 'db_vacuum'
-- target_summary: human-readable path or identifier (may be truncated)
-- outcome:        'applied' | 'proposed' | 'refused' | 'failed'
-- autonomous:     1 when triggered by _maybe_autonomous_owned_maintenance()
-- trash_ref:      absolute path to trash entry (workspace_gc only)

PRAGMA user_version = 28;

CREATE TABLE IF NOT EXISTS owned_resource_history (
    id              TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    op_type         TEXT    NOT NULL,
    target_summary  TEXT    NOT NULL,
    outcome         TEXT    NOT NULL CHECK (outcome IN ('applied', 'proposed', 'refused', 'failed')),
    autonomous      INTEGER NOT NULL DEFAULT 0,
    trash_ref       TEXT,
    created_at      INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_owned_resource_history_created_at
    ON owned_resource_history(created_at DESC);
