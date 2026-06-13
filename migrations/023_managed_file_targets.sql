-- Migration 023: operator file-target allowlist (Milestone 3 — "The being as the machine's operator").
--
-- One row per (user, target_path) — the owner-declared minimum-capability allowlist.
-- The being can only propose/apply edits to paths explicitly allowlisted here.
-- validator_cmd is the unfakeable oracle; a target with no validator is ineligible
-- (enforced in kernel.operator.ReversibleFileEdit construction, not here).
-- Populated by the owner via the manage_file conversational surface (Step 6).

PRAGMA user_version = 23;

CREATE TABLE IF NOT EXISTS managed_file_targets (
    id            TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id       TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_path   TEXT    NOT NULL,
    validator_cmd TEXT    NOT NULL,
    created_at    INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE(user_id, target_path)
);

CREATE INDEX IF NOT EXISTS idx_managed_file_targets_lookup
    ON managed_file_targets(user_id, target_path);
