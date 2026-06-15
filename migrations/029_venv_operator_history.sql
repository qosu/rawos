-- Migration 029: venv_operator_history table (M3 Stage 2 — R-venv operator).
--
-- Audit ledger for every venv-operator outcome. One row per
-- execute_approved_venv_op() or operate_on_venv() call that produces a
-- concrete outcome (applied or proposed — preflight failures are logged
-- but not ledgered here; they never touch the live venv).
--
-- op_type:             'dep_update' (future: 'dep_remove', 'dep_pin_upgrade')
-- frozen_hash_before:  sha256(pip freeze) of live venv BEFORE swap
-- frozen_hash_after:   sha256(pip freeze) of candidate venv (post-install)
-- outcome:             'applied' | 'proposed' | 'liveness_failed' | 'preflight_failed'
-- autonomous:          1 when triggered autonomously (reserved; always 0 in Stage 2
--                      since I-VENV6 forbids autonomous scan)
-- deadman_unit:        systemd timer unit name for cross-referencing revert events

PRAGMA user_version = 29;

CREATE TABLE IF NOT EXISTS venv_operator_history (
    id                  TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    op_type             TEXT    NOT NULL,
    frozen_hash_before  TEXT    NOT NULL,
    frozen_hash_after   TEXT    NOT NULL,
    outcome             TEXT    NOT NULL CHECK (outcome IN (
                                    'applied', 'proposed',
                                    'liveness_failed', 'preflight_failed'
                                )),
    autonomous          INTEGER NOT NULL DEFAULT 0,
    deadman_unit        TEXT,
    created_at          INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_venv_operator_history_created_at
    ON venv_operator_history(created_at DESC);
