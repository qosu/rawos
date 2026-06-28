"""
anima Study Tracker — Phase 11.

Computes study metrics from raw DB data and tracks hypothesis progress.
Also manages daily snapshots for research analysis.

Data sources:
  inference_log      — every intent inference (source, confidence, timeliness, domain)
  proactive_artifacts — every proactive artifact created
  artifact_ratings   — user ratings (1-5) with linking to inferences
"""
from __future__ import annotations

import json
import logging
from datetime import date

import anima.db as db
from anima.study.config import get_config, get_study_day

log = logging.getLogger("anima.study.tracker")


def _row_to_dict(row) -> dict:
    return dict(row) if row else {}


def compute_basic_stats(user_id: str | None = None) -> dict:
    """Core study statistics from all tables."""
    uid_filter = "AND user_id = ?" if user_id else ""
    params_u = (user_id,) if user_id else ()

    with db._conn() as conn:
        total_inf = conn.execute(
            f"SELECT COUNT(*) FROM inference_log WHERE 1=1 {uid_filter}", params_u
        ).fetchone()[0]

        total_art = conn.execute(
            f"SELECT COUNT(*) FROM proactive_artifacts WHERE 1=1 {uid_filter}", params_u
        ).fetchone()[0]

        total_rated = conn.execute(
            f"SELECT COUNT(*) FROM artifact_ratings WHERE 1=1 {uid_filter}", params_u
        ).fetchone()[0]

        # Precision @3: fraction of rated artifacts with rating >= 3
        at3 = conn.execute(
            f"SELECT COUNT(*) FROM artifact_ratings WHERE rating >= 3 {uid_filter}", params_u
        ).fetchone()[0] if total_rated > 0 else 0

        avg_rating_row = conn.execute(
            f"SELECT AVG(rating) FROM artifact_ratings WHERE 1=1 {uid_filter}", params_u
        ).fetchone()
        avg_rating = float(avg_rating_row[0]) if avg_rating_row[0] else None

        avg_conf_row = conn.execute(
            f"SELECT AVG(confidence) FROM inference_log WHERE 1=1 {uid_filter}", params_u
        ).fetchone()
        avg_conf = float(avg_conf_row[0]) if avg_conf_row[0] else None

        avg_tl_row = conn.execute(
            f"SELECT AVG(timeliness_score) FROM inference_log "
            f"WHERE timeliness_score IS NOT NULL {uid_filter}", params_u
        ).fetchone()
        avg_timeliness = float(avg_tl_row[0]) if avg_tl_row[0] else None

        # Source breakdown
        src_rows = conn.execute(
            f"SELECT source, COUNT(*) as n FROM inference_log "
            f"WHERE 1=1 {uid_filter} GROUP BY source", params_u
        ).fetchall()
        source_breakdown = {r["source"]: r["n"] for r in src_rows}

        # Domain breakdown (from proactive_artifacts)
        _domain_uid = "AND a.user_id = ?" if user_id else ""
        domain_rows = conn.execute(
            f"SELECT a.inferred_domain, COUNT(*) as n FROM inference_log a "
            f"JOIN proactive_artifacts p ON p.goal = a.inferred_goal "
            f"WHERE 1=1 {_domain_uid} GROUP BY a.inferred_domain",
            params_u,
        ).fetchall()
        domain_breakdown = {r["inferred_domain"]: r["n"] for r in domain_rows}

        # Context events count (activity level)
        ctx_count = conn.execute(
            f"SELECT COUNT(*) FROM context_events WHERE 1=1 {uid_filter}", params_u
        ).fetchone()[0]

    precision_at_3 = round(at3 / total_rated, 4) if total_rated > 0 else None
    coverage = round(total_rated / total_art, 4) if total_art > 0 else None

    return {
        "total_inferences":   total_inf,
        "total_artifacts":    total_art,
        "total_rated":        total_rated,
        "rating_coverage":    coverage,
        "precision_at_3":     precision_at_3,
        "avg_rating":         round(avg_rating, 3) if avg_rating else None,
        "avg_confidence":     round(avg_conf, 3) if avg_conf else None,
        "avg_timeliness":     round(avg_timeliness, 3) if avg_timeliness else None,
        "source_breakdown":   source_breakdown,
        "domain_breakdown":   domain_breakdown,
        "context_events":     ctx_count,
    }


def compute_hypothesis_status(stats: dict) -> dict:
    """
    Evaluate each hypothesis against current data.
    Returns {H1: {status, current_value, target, samples, sufficient}, ...}
    """
    results: dict[str, dict] = {}

    with db._conn() as conn:
        # H2: classifier precision vs rule precision
        clf_rated = conn.execute(
            """SELECT AVG(CASE WHEN ar.rating >= 3 THEN 1.0 ELSE 0.0 END) as prec
               FROM inference_log il
               JOIN proactive_artifacts pa ON pa.goal = il.inferred_goal
               JOIN artifact_ratings ar ON ar.proactive_artifact_id = pa.id
               WHERE il.source = 'classifier'"""
        ).fetchone()[0]

        rule_rated = conn.execute(
            """SELECT AVG(CASE WHEN ar.rating >= 3 THEN 1.0 ELSE 0.0 END) as prec
               FROM inference_log il
               JOIN proactive_artifacts pa ON pa.goal = il.inferred_goal
               JOIN artifact_ratings ar ON ar.proactive_artifact_id = pa.id
               WHERE il.source = 'rule'"""
        ).fetchone()[0]

        # H3: Spearman correlation timeliness_score vs rating
        tl_rating_rows = conn.execute(
            """SELECT il.timeliness_score, ar.rating
               FROM inference_log il
               JOIN proactive_artifacts pa ON pa.goal = il.inferred_goal
               JOIN artifact_ratings ar ON ar.proactive_artifact_id = pa.id
               WHERE il.timeliness_score IS NOT NULL"""
        ).fetchall()

        # H4: optimal threshold analysis
        threshold_rows = conn.execute(
            """SELECT il.timeliness_score,
                      CASE WHEN ar.rating >= 3 THEN 1 ELSE 0 END as accepted
               FROM inference_log il
               JOIN proactive_artifacts pa ON pa.goal = il.inferred_goal
               JOIN artifact_ratings ar ON ar.proactive_artifact_id = pa.id
               WHERE il.timeliness_score IS NOT NULL
               ORDER BY il.timeliness_score"""
        ).fetchall()

    # H1
    n1 = stats["total_rated"]
    p1 = stats["precision_at_3"]
    h1_sufficient = n1 >= int(get_config("min_rated_for_h1", "20"))
    results["H1"] = {
        "current_value": p1,
        "target": 0.65,
        "samples": n1,
        "sufficient": h1_sufficient,
        "status": _h_status(p1, 0.65, h1_sufficient),
    }

    # H2
    delta = None
    n2 = 0
    if clf_rated is not None and rule_rated is not None:
        delta = round(float(clf_rated) - float(rule_rated), 4)
        with db._conn() as conn:
            n2 = conn.execute(
                """SELECT COUNT(*) FROM inference_log il
                   JOIN proactive_artifacts pa ON pa.goal = il.inferred_goal
                   JOIN artifact_ratings ar ON ar.proactive_artifact_id = pa.id
                   WHERE il.source IN ('classifier','rule')"""
            ).fetchone()[0]
    h2_sufficient = n2 >= 10
    results["H2"] = {
        "current_value": delta,
        "target": 0.05,
        "samples": n2,
        "sufficient": h2_sufficient,
        "status": _h_status(delta, 0.05, h2_sufficient),
        "classifier_precision": round(float(clf_rated), 4) if clf_rated else None,
        "rule_precision": round(float(rule_rated), 4) if rule_rated else None,
    }

    # H3: Spearman rank correlation
    spearman = None
    n3 = len(tl_rating_rows)
    if n3 >= 5:
        try:
            from scipy.stats import spearmanr
            xs = [r["timeliness_score"] for r in tl_rating_rows]
            ys = [r["rating"] for r in tl_rating_rows]
            rho, pval = spearmanr(xs, ys)
            spearman = {"rho": round(float(rho), 4), "pval": round(float(pval), 4)}
        except ImportError:
            # scipy not available — use manual rank correlation
            spearman = {"rho": None, "pval": None, "note": "scipy not installed"}
    h3_sufficient = n3 >= 20
    results["H3"] = {
        "current_value": spearman["rho"] if spearman else None,
        "target": 0.30,
        "samples": n3,
        "sufficient": h3_sufficient,
        "status": _h_status(spearman["rho"] if spearman else None, 0.30, h3_sufficient),
        "spearman": spearman,
    }

    # H4: optimal threshold analysis
    h4_value = None
    n4 = len(threshold_rows)
    if n4 >= 10:
        # Bin by timeliness score, find which bin has highest acceptance rate
        bins = [(0.0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 1.0)]
        bin_stats = []
        for lo, hi in bins:
            in_bin = [r["accepted"] for r in threshold_rows
                      if lo <= r["timeliness_score"] < hi]
            if in_bin:
                rate = sum(in_bin) / len(in_bin)
                bin_stats.append({"range": f"{lo:.1f}-{hi:.1f}", "n": len(in_bin), "acceptance": round(rate, 4)})
        if bin_stats:
            best_bin = max(bin_stats, key=lambda x: x["acceptance"])
            best_threshold = float(best_bin["range"].split("-")[0])
            h4_value = 0.35 <= best_threshold <= 0.65
    h4_sufficient = n4 >= 30
    results["H4"] = {
        "current_value": h4_value,
        "target": True,
        "samples": n4,
        "sufficient": h4_sufficient,
        "status": _h_status(h4_value, True, h4_sufficient),
    }

    return results


def _h_status(value, target, sufficient: bool) -> str:
    if not sufficient:
        return "COLLECTING"
    if value is None:
        return "NO_DATA"
    if isinstance(target, bool):
        return "CONFIRMED" if value == target else "REFUTED"
    if isinstance(target, float) and isinstance(value, float):
        return "CONFIRMED" if value >= target else "REFUTED"
    return "UNKNOWN"


def take_daily_snapshot(user_id: str | None = None) -> str:
    """Compute and store a daily snapshot. Returns the snapshot id."""
    today = date.today().isoformat()
    stats = compute_basic_stats(user_id)

    with db._conn() as conn:
        row = conn.execute(
            """INSERT INTO study_daily_snapshots
               (snapshot_date, user_id, total_inferences, total_artifacts,
                total_rated, precision_at_3, avg_rating, avg_confidence,
                avg_timeliness, source_breakdown, domain_breakdown)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(snapshot_date, user_id) DO UPDATE SET
                 total_inferences = excluded.total_inferences,
                 total_artifacts  = excluded.total_artifacts,
                 total_rated      = excluded.total_rated,
                 precision_at_3   = excluded.precision_at_3,
                 avg_rating       = excluded.avg_rating,
                 avg_confidence   = excluded.avg_confidence,
                 avg_timeliness   = excluded.avg_timeliness,
                 source_breakdown = excluded.source_breakdown,
                 domain_breakdown = excluded.domain_breakdown,
                 snapshot_at      = unixepoch()
               RETURNING id""",
            (
                today, user_id,
                stats["total_inferences"], stats["total_artifacts"],
                stats["total_rated"],
                stats["precision_at_3"], stats["avg_rating"],
                stats["avg_confidence"], stats["avg_timeliness"],
                json.dumps(stats["source_breakdown"]),
                json.dumps(stats["domain_breakdown"]),
            ),
        ).fetchone()
    snap_id = row["id"] if row else "?"
    log.info("daily snapshot saved: %s (day=%d)", today, get_study_day())
    return snap_id


def get_daily_history(limit: int = 30) -> list[dict]:
    """Return daily snapshots for trend charts."""
    with db._conn() as conn:
        rows = conn.execute(
            """SELECT * FROM study_daily_snapshots
               ORDER BY snapshot_date DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_full_report(user_id: str | None = None) -> dict:
    """Complete research study report."""
    stats = compute_basic_stats(user_id)
    hypotheses = compute_hypothesis_status(stats)
    history = get_daily_history()
    study_day = get_study_day()
    start_date = get_config("study_start_date", "unknown")
    target_days = int(get_config("study_duration_days", "30"))

    # Context events per domain (from context_events)
    with db._conn() as conn:
        watched = conn.execute(
            "SELECT user_id, path, label FROM watched_paths WHERE active = 1"
        ).fetchall()

    return {
        "study_day":    study_day,
        "study_start":  start_date,
        "target_days":  target_days,
        "progress_pct": round(study_day / target_days * 100, 1),
        "stats":        stats,
        "hypotheses":   hypotheses,
        "history":      history[:14],   # last 2 weeks for report
        "watched_paths": [{"path": r["path"], "label": r["label"]} for r in watched],
    }
