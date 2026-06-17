-- Migration 030: unit topology target allowlist (Phase 23-full)
-- Stores the owner-allowlisted (user_id, unit_name) pairs that the being is
-- permitted to operate on via operate_on_unit_topology().
-- No validator_cmd column — systemd-analyze verify is always the pre-flight oracle (I-UT5).
PRAGMA user_version = 30;

CREATE TABLE IF NOT EXISTS managed_unit_targets (
    id         TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id    TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    unit_name  TEXT    NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE(user_id, unit_name)
);

CREATE INDEX IF NOT EXISTS idx_managed_unit_targets_lookup
    ON managed_unit_targets(user_id, unit_name);
