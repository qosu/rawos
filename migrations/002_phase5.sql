-- Migration 002 — Phase 5: admin flag, billing events, token tracking

-- Add is_admin to users (idempotent: error caught by _apply_schema if already exists)
ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0;

-- Billing events: immutable log of every token consumption event
CREATE TABLE IF NOT EXISTS billing_events (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    intent_id   TEXT,
    tokens      INTEGER NOT NULL DEFAULT 0,
    model       TEXT NOT NULL DEFAULT '',
    event_type  TEXT NOT NULL DEFAULT 'intent',
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_billing_events_user_time
    ON billing_events(user_id, created_at DESC);
