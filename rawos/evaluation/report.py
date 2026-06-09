"""
rawos Evaluation Report — Phase 7.

Aggregates inference_log + artifact_ratings into research-grade statistics.

Metrics produced:
  precision          — correct_rated / total_rated  (where correct = rating >= 3)
  relevance_mean     — mean rating across all rated artifacts
  relevance_dist     — count per rating level (1-5)
  total_inferences   — all inferences ever made
  total_rated        — inferences with known outcome
  confidence_bins    — precision per confidence bucket (0-25%, 25-50%, 50-75%, 75-100%)
  domain_breakdown   — precision + mean relevance per domain
  source_breakdown   — precision per inference source (rule vs llm)
"""
from __future__ import annotations

import logging
from typing import Any

import rawos.db as db

log = logging.getLogger("rawos.evaluation.report")


def get_report(user_id: str) -> dict[str, Any]:
    with db._conn() as conn:
        # Total inferences
        total_inf = conn.execute(
            "SELECT COUNT(*) as n FROM inference_log WHERE user_id = ?",
            (user_id,),
        ).fetchone()["n"]

        # Rated inferences (linked to rated artifacts)
        rated = conn.execute(
            """SELECT COUNT(*) as n FROM inference_log il
               JOIN artifact_ratings ar ON il.proactive_artifact_id = ar.proactive_artifact_id
               WHERE il.user_id = ? AND il.correct IS NOT NULL""",
            (user_id,),
        ).fetchone()["n"]

        # Correct inferences (rating >= 3 → correct = 1)
        correct = conn.execute(
            "SELECT COUNT(*) as n FROM inference_log WHERE user_id = ? AND correct = 1",
            (user_id,),
        ).fetchone()["n"]

        # Relevance mean + distribution
        rel_rows = conn.execute(
            "SELECT rating, COUNT(*) as n FROM artifact_ratings WHERE user_id = ? GROUP BY rating",
            (user_id,),
        ).fetchall()
        rel_dist = {str(r["rating"]): r["n"] for r in rel_rows}
        total_ratings = sum(rel_dist.values())
        rel_mean = (
            conn.execute(
                "SELECT AVG(rating) as m FROM artifact_ratings WHERE user_id = ?",
                (user_id,),
            ).fetchone()["m"] or 0.0
        )

        # Confidence bucket precision
        conf_rows = conn.execute(
            """SELECT
                 CASE
                   WHEN confidence < 0.50 THEN '0-50%'
                   WHEN confidence < 0.65 THEN '50-65%'
                   WHEN confidence < 0.80 THEN '65-80%'
                   ELSE '80-100%'
                 END as bucket,
                 COUNT(*) as total,
                 SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) as correct_n
               FROM inference_log
               WHERE user_id = ? AND correct IS NOT NULL
               GROUP BY bucket""",
            (user_id,),
        ).fetchall()
        confidence_bins = {
            r["bucket"]: {
                "total": r["total"],
                "correct": r["correct_n"],
                "precision": round(r["correct_n"] / r["total"], 3) if r["total"] else None,
            }
            for r in conf_rows
        }

        # Domain breakdown
        domain_rows = conn.execute(
            """SELECT il.inferred_domain,
                      COUNT(*) as inferences,
                      SUM(CASE WHEN il.correct = 1 THEN 1 ELSE 0 END) as correct_n,
                      COUNT(CASE WHEN il.correct IS NOT NULL THEN 1 END) as rated_n,
                      AVG(ar.rating) as mean_rating
               FROM inference_log il
               LEFT JOIN artifact_ratings ar ON il.proactive_artifact_id = ar.proactive_artifact_id
               WHERE il.user_id = ?
               GROUP BY il.inferred_domain""",
            (user_id,),
        ).fetchall()
        domain_breakdown = {
            r["inferred_domain"]: {
                "inferences": r["inferences"],
                "precision": round(r["correct_n"] / r["rated_n"], 3) if r["rated_n"] else None,
                "mean_rating": round(r["mean_rating"], 2) if r["mean_rating"] else None,
            }
            for r in domain_rows
        }

        # Source breakdown (rule vs llm)
        src_rows = conn.execute(
            """SELECT source,
                      COUNT(*) as total,
                      SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END) as correct_n,
                      COUNT(CASE WHEN correct IS NOT NULL THEN 1 END) as rated_n
               FROM inference_log
               WHERE user_id = ?
               GROUP BY source""",
            (user_id,),
        ).fetchall()
        source_breakdown = {
            r["source"]: {
                "total": r["total"],
                "precision": round(r["correct_n"] / r["rated_n"], 3) if r["rated_n"] else None,
            }
            for r in src_rows
        }

    precision = round(correct / rated, 3) if rated > 0 else None

    return {
        "total_inferences":  total_inf,
        "total_rated":       rated,
        "total_ratings":     total_ratings,
        "precision":         precision,
        "relevance_mean":    round(rel_mean, 3) if rel_mean else None,
        "relevance_dist":    rel_dist,
        "confidence_bins":   confidence_bins,
        "domain_breakdown":  domain_breakdown,
        "source_breakdown":  source_breakdown,
        "threshold_target":  0.65,
        "relevance_target":  3.0,
        "status": (
            "on_target"    if precision is not None and precision >= 0.65 and rel_mean >= 3.0
            else "below_target" if precision is not None
            else "insufficient_data"
        ),
    }
