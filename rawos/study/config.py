"""
rawos Study Configuration — Phase 11.

Research hypotheses and target metrics for the 30-day user study.
Hypothesis validation drives the paper (arXiv → UIST/CHI 2027).
"""
from __future__ import annotations

import logging

import rawos.db as db

log = logging.getLogger("rawos.study.config")

STUDY_DURATION_DAYS: int = 30

# Research hypotheses: id → {text, metric, target, min_samples, description}
HYPOTHESES: dict[str, dict] = {
    "H1": {
        "text": "Zero-interruption proactive AI achieves precision ≥65% "
                "(artifacts rated ≥3/5 by the researcher)",
        "metric":      "precision_at_3",
        "target":      0.65,
        "min_samples": 20,
        "description": "Core thesis: the system is net positive above 65% precision",
    },
    "H2": {
        "text": "ML classifier achieves higher real-world precision than rule-based inference",
        "metric":      "classifier_vs_rule_precision_delta",
        "target":      0.05,   # classifier at least 5% better
        "min_samples": 10,
        "description": "Validates Phase 9 classifier against production rule baseline",
    },
    "H3": {
        "text": "Timing model correlates positively with artifact acceptance "
                "(Spearman ρ ≥ 0.3 between timeliness_score and rating)",
        "metric":      "timing_acceptance_spearman",
        "target":      0.30,
        "min_samples": 20,
        "description": "Validates Phase 10 timing model improves acceptance",
    },
    "H4": {
        "text": "The optimal timeliness threshold for net-positive experience "
                "lies in [0.35, 0.65]",
        "metric":      "optimal_threshold_in_range",
        "target":      True,
        "min_samples": 30,
        "description": "Identifies the correct operating point for the timing gate",
    },
}


def get_config(key: str, default: str = "") -> str:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT value FROM study_config WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_config(key: str, value: str) -> None:
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO study_config (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
               updated_at = unixepoch()""",
            (key, value),
        )


def get_study_day() -> int:
    """Return current study day (1-based). Returns 0 before study start."""
    import time
    from datetime import date, datetime
    start_str = get_config("study_start_date", "")
    if not start_str:
        return 0
    try:
        start = date.fromisoformat(start_str)
        today = date.today()
        delta = (today - start).days + 1
        return max(0, min(delta, STUDY_DURATION_DAYS))
    except ValueError:
        return 0
