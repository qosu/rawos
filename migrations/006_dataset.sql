-- labeled_examples: ground truth dataset for intent inference evaluation
-- behavioral_context matches the schema that intent_engine.py reads from user_model
-- true_goal / true_domain are the labels for supervised evaluation
CREATE TABLE IF NOT EXISTS labeled_examples (
    id                    TEXT    PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    source                TEXT    NOT NULL CHECK(source IN ('extracted', 'synthetic', 'rated')),
    behavioral_context    TEXT    NOT NULL,   -- JSON: {inferred_stack, active_domains, recent_activity, project_count, artifact_count}
    true_goal             TEXT    NOT NULL,
    true_domain           TEXT    NOT NULL,
    expected_confidence   REAL,              -- confidence the inference engine should produce, 0.0–1.0
    quality_score         INTEGER DEFAULT 3 CHECK(quality_score BETWEEN 1 AND 5),
    created_at            INTEGER NOT NULL DEFAULT (unixepoch()),
    notes                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_labeled_examples_domain  ON labeled_examples(true_domain);
CREATE INDEX IF NOT EXISTS idx_labeled_examples_source  ON labeled_examples(source);
CREATE INDEX IF NOT EXISTS idx_labeled_examples_ts      ON labeled_examples(created_at DESC);
