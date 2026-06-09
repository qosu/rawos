-- Migration 012: Push notification device registry
-- Phase 5: Mobile app push delivery via Expo Push API
-- Expo handles FCM (Android) + APNs (iOS) routing — no Firebase project required.

PRAGMA user_version = 12;

CREATE TABLE IF NOT EXISTS push_devices (
    id              TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id         TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expo_token      TEXT    NOT NULL,
    platform        TEXT    NOT NULL CHECK (platform IN ('ios', 'android', 'web')),
    registered_at   INTEGER NOT NULL DEFAULT (unixepoch()),
    last_active_at  INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE (user_id, expo_token)
);

CREATE INDEX IF NOT EXISTS idx_push_devices_user ON push_devices(user_id, last_active_at DESC);
