-- Migration 013: Proactive tool calls audit log
-- Phase A: proactive scheduler now uses agent_loop with real tools
-- Every tool call by the autonomous proactive agent is recorded here.

PRAGMA user_version = 13;

CREATE TABLE IF NOT EXISTS proactive_tool_calls (
    id           TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id      TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    artifact_id  TEXT    REFERENCES proactive_artifacts(id) ON DELETE SET NULL,
    tool_name    TEXT    NOT NULL,
    tool_input   TEXT    NOT NULL DEFAULT '{}',
    tool_output  TEXT    NOT NULL DEFAULT '',
    success      INTEGER NOT NULL DEFAULT 0,
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    called_at    INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_ptool_user     ON proactive_tool_calls(user_id, called_at DESC);
CREATE INDEX IF NOT EXISTS idx_ptool_artifact ON proactive_tool_calls(artifact_id);
