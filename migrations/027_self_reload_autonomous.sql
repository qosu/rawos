-- Migration 027: track whether each self-reload outcome was autonomous (Phase 25 Stage 2).
-- _apply_schema is idempotent: swallows "duplicate column" on re-run (mirrors migration 019).
PRAGMA user_version = 27;
ALTER TABLE managed_self_reload ADD COLUMN autonomous INTEGER NOT NULL DEFAULT 0;
