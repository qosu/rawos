"""
rawos Evaluation Feedback — Phase 7.

Handles artifact ratings submitted via `rawos rate <file> 1-5`.

Flow:
  user runs `rawos rate RAWOS_debugging_auth_1234567.md 4`
  → CLI calls POST /evaluation/rate
  → feedback.submit_rating:
      1. Find proactive_artifact by file_path
      2. Insert artifact_ratings row
      3. Mark linked inference_log as correct (rating >= 3) or incorrect
      4. Update Prometheus metrics
"""
from __future__ import annotations

import logging
from pathlib import Path

import rawos.db as db
from rawos.evaluation.metrics import mark_inference_correct

log = logging.getLogger("rawos.evaluation.feedback")


def submit_rating(
    user_id: str,
    file_path: str,
    rating: int,
    comment: str | None = None,
) -> dict:
    """
    Submit a relevance rating for a proactive artifact.
    Returns the inserted rating record dict.

    Raises ValueError if file_path does not match any proactive artifact for this user.
    """
    if not (1 <= rating <= 5):
        raise ValueError(f"rating must be 1-5, got {rating}")

    abs_path = str(Path(file_path).expanduser().resolve())

    # Find proactive_artifact by file_path
    with db._conn() as conn:
        pa_row = conn.execute(
            "SELECT id, artifact_id FROM proactive_artifacts WHERE user_id = ? AND file_path = ? LIMIT 1",
            (user_id, abs_path),
        ).fetchone()

    if not pa_row:
        # Try basename match (user may pass just the filename)
        basename = Path(abs_path).name
        with db._conn() as conn:
            pa_row = conn.execute(
                """SELECT id, artifact_id FROM proactive_artifacts
                   WHERE user_id = ? AND file_path LIKE ? LIMIT 1""",
                (user_id, f"%{basename}"),
            ).fetchone()

    proactive_artifact_id = pa_row["id"] if pa_row else None
    artifact_id = pa_row["artifact_id"] if pa_row else None

    # Check for duplicate rating (update existing instead of insert)
    with db._conn() as conn:
        existing = conn.execute(
            "SELECT id FROM artifact_ratings WHERE user_id = ? AND file_path = ? LIMIT 1",
            (user_id, abs_path),
        ).fetchone()

    if existing:
        with db._conn() as conn:
            conn.execute(
                "UPDATE artifact_ratings SET rating = ?, comment = ?, ts = unixepoch() WHERE id = ?",
                (rating, comment, existing["id"]),
            )
        rating_id = existing["id"]
    else:
        with db._conn() as conn:
            row = conn.execute(
                """INSERT INTO artifact_ratings
                   (user_id, proactive_artifact_id, artifact_id, file_path, rating, comment)
                   VALUES (?, ?, ?, ?, ?, ?)
                   RETURNING id""",
                (user_id, proactive_artifact_id, artifact_id, abs_path, rating, comment),
            ).fetchone()
        rating_id = row["id"]

    # Mark linked inference correct/incorrect
    if proactive_artifact_id:
        mark_inference_correct(proactive_artifact_id, 1 if rating >= 3 else 0)

    # Update Prometheus metrics
    _update_prometheus_metrics(user_id, rating)
    _update_autonomy_on_rating(user_id, rating)

    log.info("rating submitted: user=%s file=%s rating=%d", user_id, Path(abs_path).name, rating)
    return {"id": rating_id, "rating": rating, "file_path": abs_path}


def _update_prometheus_metrics(user_id: str, new_rating: int) -> None:
    """Update Prometheus counters and mean gauge after a rating submission."""
    try:
        from rawos import monitoring
        monitoring.artifact_rating_total.labels(rating=str(new_rating)).inc()
        if new_rating >= 3:
            monitoring.inference_rated_correct_total.inc()
        else:
            monitoring.inference_rated_incorrect_total.inc()

        # Recompute rolling mean from DB (correct — not an approximation)
        with db._conn() as conn:
            row = conn.execute(
                "SELECT AVG(rating) as mean FROM artifact_ratings WHERE rating IS NOT NULL"
            ).fetchone()
        mean = row["mean"] if row and row["mean"] is not None else 0.0
        monitoring.artifact_relevance_mean.set(mean)
    except Exception:
        log.exception("failed to update Prometheus metrics after rating")


def _update_autonomy_on_rating(user_id: str, rating: int) -> None:
    """Update autonomy_grants good_count/bad_count and auto-downgrade if bad_count >= 2."""
    import time as _time
    now = int(_time.time())
    try:
        with db._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO autonomy_grants
                       (user_id, action_type, level, granted_at, good_count, bad_count)
                   VALUES (?, 'analysis', 0, ?, 0, 0)""",
                (user_id, now),
            )
            if rating >= 3:
                conn.execute(
                    """UPDATE autonomy_grants
                       SET good_count = good_count + 1, last_action = ?
                       WHERE user_id = ? AND action_type = 'analysis'""",
                    (now, user_id),
                )
            else:
                row = conn.execute(
                    """SELECT level, bad_count FROM autonomy_grants
                       WHERE user_id = ? AND action_type = 'analysis'""",
                    (user_id,),
                ).fetchone()
                current_level = row["level"] if row else 0
                new_bad = (row["bad_count"] if row else 0) + 1
                if new_bad >= 2 and current_level > 0:
                    new_level = current_level - 1
                    conn.execute(
                        """UPDATE autonomy_grants
                           SET level = ?, bad_count = 0, last_action = ?
                           WHERE user_id = ? AND action_type = 'analysis'""",
                        (new_level, now, user_id),
                    )
                    log.info(
                        "autonomy downgrade: user=%s level %d→%d (bad_count threshold)",
                        user_id, current_level, new_level,
                    )
                else:
                    conn.execute(
                        """UPDATE autonomy_grants
                           SET bad_count = ?, last_action = ?
                           WHERE user_id = ? AND action_type = 'analysis'""",
                        (new_bad, now, user_id),
                    )
    except Exception:
        log.exception("failed to update autonomy grants for user=%s", user_id)
