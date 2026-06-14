-- Migration 024: operator service-target allowlist (Phase 23a — "supervisor authority").
--
-- One row per (user, service_name) — the owner-declared minimum-capability allowlist
-- for service lifecycle actions (restart/start/stop). The being can only propose/apply
-- actions on services explicitly allowlisted here.
-- validator_cmd is the unfakeable health oracle for restart/start verify (ignored by
-- stop, whose oracle is the systemd is-active verdict alone); a target with no
-- validator is ineligible (enforced in kernel.operator.ReversibleServiceAction
-- construction, not here).
-- Populated by the owner via the manage_service conversational surface (Step 6).
-- operator_track_record (migration 022) is reused as-is, keyed by
-- operation_class = "service_<action>" — no new graduation table needed.

PRAGMA user_version = 24;

CREATE TABLE IF NOT EXISTS managed_service_targets (
    id            TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id       TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    service_name  TEXT    NOT NULL,
    validator_cmd TEXT    NOT NULL,
    created_at    INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE(user_id, service_name)
);

CREATE INDEX IF NOT EXISTS idx_managed_service_targets_lookup
    ON managed_service_targets(user_id, service_name);
