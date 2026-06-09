-- Migration 011: Universal context sources — calendar + documents
-- Phase 4: opens rawos to non-developers (calendar events, document changes)

PRAGMA user_version = 11;

-- Track the source type of each context event
ALTER TABLE context_events ADD COLUMN source_type TEXT NOT NULL DEFAULT 'file';

-- Calendar events from CalDAV sync
CREATE TABLE IF NOT EXISTS calendar_events (
    id           TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id      TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    external_id  TEXT    NOT NULL,
    title        TEXT    NOT NULL,
    start_ts     INTEGER NOT NULL,
    end_ts       INTEGER NOT NULL,
    attendees    TEXT,
    location     TEXT,
    description  TEXT,
    calendar_url TEXT,
    synced_at    INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE (user_id, external_id)
);

-- Per-user CalDAV credentials (password AES-GCM encrypted with JWT secret key)
CREATE TABLE IF NOT EXISTS calendar_credentials (
    user_id       TEXT    PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    caldav_url    TEXT    NOT NULL,
    username      TEXT    NOT NULL,
    password_enc  TEXT    NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    last_sync_ts  INTEGER,
    sync_error    TEXT
);

CREATE INDEX IF NOT EXISTS idx_calendar_events_user_start
    ON calendar_events(user_id, start_ts);
