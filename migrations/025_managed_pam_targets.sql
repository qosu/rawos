-- Migration 025: PAM target allowlist (Phase 22 — "PAM safety-floor").
--
-- One row per (user, pam_file) — the owner-declared allowlist for
-- non-root-critical pam.d write authority. Being can only write pam.d files
-- that are explicitly allowlisted here AND are not in _SELF_PROTECTED_PAM_FILES.
--
-- No validator_cmd column: the oracle is always the dedicated on-box probe key
-- at /root/.rawos-pam-backups/probe_key (fixed per I5 — changing the oracle
-- per-target would allow a target to declare a weaker oracle and subvert the
-- safety guarantee). See docs/phase22_pam_invariants.md.
--
-- Snapshot storage is NOT here: PamSnapshot files live at
-- /root/.rawos-pam-backups/<uuid> (raw files, I9 — must survive rawos.db
-- being inaccessible when the deadman revert fires).

PRAGMA user_version = 25;

CREATE TABLE IF NOT EXISTS managed_pam_targets (
    id          TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id     TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    pam_file    TEXT    NOT NULL,
    created_at  INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE(user_id, pam_file)
);

CREATE INDEX IF NOT EXISTS idx_managed_pam_targets_lookup
    ON managed_pam_targets(user_id, pam_file);
