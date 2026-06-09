-- Migration 014: Autonomous git commit audit log
-- Phase D: at trust Level 2, rawos can create rawos/* branches and commit fixes.
-- Every git commit made by the autonomous agent is recorded here.

PRAGMA user_version = 14;

CREATE TABLE IF NOT EXISTS rawos_commits (
    id          TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id     TEXT    NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
    project_id  TEXT             REFERENCES projects(id) ON DELETE SET NULL,
    branch      TEXT    NOT NULL,
    commit_hash TEXT    NOT NULL,
    message     TEXT    NOT NULL DEFAULT 'rawos: autonomous fix',
    workdir     TEXT    NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_rawos_commits_user    ON rawos_commits(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rawos_commits_project ON rawos_commits(project_id);
