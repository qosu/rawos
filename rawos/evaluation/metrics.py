"""
rawos Evaluation Metrics — Phase 7.

Low-level logging functions. Called by proactive scheduler and feedback handler.
All writes are synchronous (SQLite is thread-safe in WAL mode).
"""
from __future__ import annotations

import logging
import time

import rawos.db as db

log = logging.getLogger("rawos.evaluation.metrics")


def log_inference(
    user_id: str,
    goal: str,
    domain: str,
    confidence: float,
    source: str,
    timeliness_score: float | None = None,
    timing_signals: str | None = None,
) -> str:
    """
    Log one intent inference. Returns inference_log id.
    Called EVERY time intent_engine produces a result, regardless of whether
    it leads to a proactive artifact.
    """
    with db._conn() as conn:
        row = conn.execute(
            """INSERT INTO inference_log
               (user_id, inferred_goal, inferred_domain, confidence, source,
                timeliness_score, timing_signals)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               RETURNING id""",
            (user_id, goal[:500], domain[:64], confidence, source[:16],
             timeliness_score, timing_signals),
        ).fetchone()
    inference_id = row["id"]
    log.debug("logged inference %s goal='%s' conf=%.2f", inference_id[:8], goal[:40], confidence)
    return inference_id


def link_inference_to_artifact(inference_id: str, proactive_artifact_id: str) -> None:
    """
    After a proactive artifact is created, link the inference that triggered it.
    This is what allows artifact ratings to mark inferences as correct/incorrect.
    """
    with db._conn() as conn:
        conn.execute(
            "UPDATE inference_log SET proactive_artifact_id = ? WHERE id = ?",
            (proactive_artifact_id, inference_id),
        )


def mark_inference_correct(proactive_artifact_id: str, correct: int) -> None:
    """
    Mark all inference_log entries linked to this artifact as correct (1) or incorrect (0).
    Called by feedback.submit_rating.
    """
    with db._conn() as conn:
        conn.execute(
            "UPDATE inference_log SET correct = ? WHERE proactive_artifact_id = ?",
            (correct, proactive_artifact_id),
        )
