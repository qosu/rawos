-- Migration 015: Add agent_id to proactive_tool_calls audit log
-- Allows correlating tool calls with the specific agent run that made them.

PRAGMA user_version = 15;

ALTER TABLE proactive_tool_calls ADD COLUMN agent_id TEXT REFERENCES agents(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_ptool_agent ON proactive_tool_calls(agent_id);
