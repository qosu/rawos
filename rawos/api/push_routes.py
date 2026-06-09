"""
rawos Push Notification API routes.

POST   /push/register                   — register Expo push token for current user
DELETE /push/unregister                 — remove push token
GET    /push/artifacts                  — list recent proactive artifacts (mobile feed)
GET    /push/artifacts/{artifact_id}/content — full text content of artifact file
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import rawos.db as db
from rawos.api.deps import current_user
from rawos.models import User

log = logging.getLogger("rawos.push.routes")
router = APIRouter()


# ---------------------------------------------------------------------------
# Device registration
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    expo_token: str = Field(..., min_length=10, max_length=200)
    platform:   str = Field(..., pattern=r"^(ios|android|web)$")


@router.post("/push/register", status_code=200)
async def register_device(body: RegisterRequest, user: User = Depends(current_user)):
    """Register or refresh an Expo push token for this user."""
    now = int(time.time())
    try:
        with db._conn() as conn:
            conn.execute(
                """INSERT INTO push_devices (user_id, expo_token, platform, registered_at, last_active_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT (user_id, expo_token) DO UPDATE SET
                       last_active_at = excluded.last_active_at,
                       platform       = excluded.platform""",
                (user.id, body.expo_token, body.platform, now, now),
            )
    except Exception:
        log.exception("register_device failed user=%s", user.id)
        raise HTTPException(status_code=500, detail="device registration failed")
    return {"registered": True}


@router.delete("/push/unregister", status_code=200)
async def unregister_device(
    expo_token: str = Query(..., min_length=10),
    user: User = Depends(current_user),
):
    """Remove a push token for this user."""
    with db._conn() as conn:
        conn.execute(
            "DELETE FROM push_devices WHERE user_id = ? AND expo_token = ?",
            (user.id, expo_token),
        )
    return {"unregistered": True}


# ---------------------------------------------------------------------------
# Artifact feed for mobile
# ---------------------------------------------------------------------------

@router.get("/push/artifacts")
async def list_push_artifacts(
    limit: int = Query(default=20, ge=1, le=100),
    user:  User = Depends(current_user),
):
    """
    Return recent proactive artifacts for the authenticated user.
    Includes rating if one exists. Used by the mobile app artifact feed.
    """
    with db._conn() as conn:
        rows = conn.execute(
            """SELECT
                   pa.id,
                   pa.goal,
                   pa.confidence,
                   pa.action_type,
                   pa.created_at,
                   ar.rating,
                   ar.comment
               FROM proactive_artifacts pa
               LEFT JOIN artifact_ratings ar
                   ON ar.proactive_artifact_id = pa.id AND ar.user_id = pa.user_id
               WHERE pa.user_id = ?
               ORDER BY pa.created_at DESC
               LIMIT ?""",
            (user.id, limit),
        ).fetchall()

    return {
        "artifacts": [
            {
                "id":          r["id"],
                "goal":        r["goal"],
                "confidence":  r["confidence"],
                "action_type": r["action_type"],
                "created_at":  r["created_at"],
                "rating":      r["rating"],
                "comment":     r["comment"],
            }
            for r in rows
        ],
        "count": len(rows),
    }


@router.get("/push/artifacts/{artifact_id}/content")
async def get_artifact_content(
    artifact_id: str,
    user: User = Depends(current_user),
):
    """
    Return the full text content of a proactive artifact file.
    Verifies ownership. Used by mobile artifact detail screen.
    """
    with db._conn() as conn:
        row = conn.execute(
            """SELECT pa.id, pa.goal, pa.file_path, pa.action_type, pa.created_at,
                      ar.rating
               FROM proactive_artifacts pa
               LEFT JOIN artifact_ratings ar
                   ON ar.proactive_artifact_id = pa.id AND ar.user_id = pa.user_id
               WHERE pa.id = ? AND pa.user_id = ?""",
            (artifact_id, user.id),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="artifact not found")

    file_path = row["file_path"]
    try:
        content = Path(file_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="artifact file not found on disk")
    except Exception:
        log.exception("get_artifact_content: read failed path=%s", file_path)
        raise HTTPException(status_code=500, detail="failed to read artifact")

    return {
        "id":          row["id"],
        "goal":        row["goal"],
        "file_path":   file_path,
        "action_type": row["action_type"],
        "created_at":  row["created_at"],
        "rating":      row["rating"],
        "content":     content,
    }
