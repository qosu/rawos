-- inference_log: every intent inference the engine produces
-- linked to proactive_artifacts when one is created from the inference
CREATE TABLE IF NOT EXISTS inference_log (
    id                    TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id               TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    inferred_goal         TEXT    NOT NULL,
    inferred_domain       TEXT    NOT NULL,
    confidence            REAL    NOT NULL,
    source                TEXT    NOT NULL DEFAULT 'rule',  -- rule | llm
    proactive_artifact_id TEXT    REFERENCES proactive_artifacts(id) ON DELETE SET NULL,
    correct               INTEGER,   -- NULL=unrated  1=correct  0=incorrect
    ground_truth          TEXT,
    ts                    INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_inference_log_user_ts ON inference_log(user_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_inference_log_artifact  ON inference_log(proactive_artifact_id);

-- artifact_ratings: user feedback via rawos rate <file> 1-5
CREATE TABLE IF NOT EXISTS artifact_ratings (
    id                    TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id               TEXT    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    proactive_artifact_id TEXT    REFERENCES proactive_artifacts(id) ON DELETE SET NULL,
    artifact_id           TEXT    REFERENCES artifacts(id)           ON DELETE SET NULL,
    file_path             TEXT    NOT NULL,
    rating                INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    comment               TEXT,
    ts                    INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_artifact_ratings_user ON artifact_ratings(user_id, ts DESC);
