"""
rawos Study API Routes — Phase 11.

POST /study/setup    — register watched paths, start data collection
GET  /study/status   — current study state (day, data counts)
GET  /study/report   — full research report (hypotheses, metrics, trends)
GET  /study/daily    — daily snapshot history
POST /study/snapshot — manually trigger a daily snapshot
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Body, Depends

from rawos.api.deps import current_user
from rawos.models import User

log = logging.getLogger("rawos.api.study")
router = APIRouter(prefix="/study")


@router.post("/setup")
def study_setup(
    paths: Annotated[list[str], Body(embed=True)],
    labels: Annotated[list[str] | None, Body(embed=True)] = None,
    user: User = Depends(current_user),
) -> dict:
    """
    Register filesystem paths for personal watching.
    Paths will be monitored for file changes and mapped to context_events for this user.
    Triggers hot-reload of the personal filesystem watcher.
    """
    import rawos.db as db
    from rawos.context.collector import reload_personal_watcher

    if not paths:
        return {"error": "no paths provided"}

    registered: list[str] = []
    skipped: list[str] = []

    for i, raw_path in enumerate(paths):
        p = Path(raw_path).expanduser().resolve()
        if not p.exists():
            skipped.append(f"{raw_path} (does not exist)")
            continue
        if not p.is_dir():
            skipped.append(f"{raw_path} (not a directory)")
            continue
        label = (labels[i] if labels and i < len(labels) else "") or p.name
        try:
            with db._conn() as conn:
                conn.execute(
                    """INSERT INTO watched_paths (user_id, path, label)
                       VALUES (?, ?, ?)
                       ON CONFLICT(user_id, path) DO UPDATE SET
                         label = excluded.label, active = 1""",
                    (user.id, str(p), label),
                )
            registered.append(str(p))
        except Exception as exc:
            skipped.append(f"{raw_path} ({exc})")

    if registered:
        try:
            reload_personal_watcher()
            log.info("personal watcher reloaded after setup: %d paths", len(registered))
        except Exception as exc:
            log.warning("watcher reload failed: %s", exc)

    return {
        "registered": registered,
        "skipped": skipped,
        "total_watched": len(registered),
        "message": f"Watching {len(registered)} path(s). File events will now generate context data.",
    }


@router.get("/status")
def study_status(user: User = Depends(current_user)) -> dict:
    """Quick study status: day, data counts, watched paths."""
    import rawos.db as db
    from rawos.study.config import get_study_day, get_config

    with db._conn() as conn:
        ctx_count = conn.execute(
            "SELECT COUNT(*) FROM context_events WHERE user_id = ?", (user.id,)
        ).fetchone()[0]
        inf_count = conn.execute(
            "SELECT COUNT(*) FROM inference_log WHERE user_id = ?", (user.id,)
        ).fetchone()[0]
        art_count = conn.execute(
            "SELECT COUNT(*) FROM proactive_artifacts WHERE user_id = ?", (user.id,)
        ).fetchone()[0]
        rated_count = conn.execute(
            "SELECT COUNT(*) FROM artifact_ratings WHERE user_id = ?", (user.id,)
        ).fetchone()[0]
        watched_rows = conn.execute(
            "SELECT path, label FROM watched_paths WHERE user_id = ? AND active = 1",
            (user.id,),
        ).fetchall()

    return {
        "study_day":      get_study_day(),
        "study_start":    get_config("study_start_date"),
        "context_events": ctx_count,
        "inferences":     inf_count,
        "artifacts":      art_count,
        "rated":          rated_count,
        "watched_paths":  [{"path": r["path"], "label": r["label"]} for r in watched_rows],
        "data_flowing":   ctx_count > 0,
    }


@router.get("/report")
def study_report(user: User = Depends(current_user)) -> dict:
    """Full research report: hypotheses, metrics, temporal trends."""
    from rawos.study.tracker import get_full_report
    return get_full_report(user_id=user.id)


@router.get("/daily")
def study_daily(limit: int = 30, user: User = Depends(current_user)) -> dict:
    """Daily snapshot history for trend charts."""
    from rawos.study.tracker import get_daily_history
    history = get_daily_history(limit=limit)
    return {"count": len(history), "snapshots": history}


@router.post("/snapshot")
def study_snapshot(user: User = Depends(current_user)) -> dict:
    """Manually trigger a daily snapshot."""
    from rawos.study.tracker import take_daily_snapshot
    snap_id = take_daily_snapshot(user_id=user.id)
    return {"status": "ok", "snapshot_id": snap_id}
