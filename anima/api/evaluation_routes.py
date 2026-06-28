"""
anima Evaluation API routes — Phase 7.

POST /evaluation/rate      — submit artifact relevance rating (1-5)
GET  /evaluation/report    — precision, relevance, breakdown stats
GET  /evaluation/inferences — recent inference log (debugging + research)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from anima.api.deps import current_user
from anima.evaluation.feedback import submit_rating
from anima.evaluation.report import get_report
import anima.db as db

log = logging.getLogger("anima.evaluation")
router = APIRouter()


class RateRequest(BaseModel):
    file_path: str = Field(..., description="Absolute or relative path to the RAWOS_ file")
    rating:    int = Field(..., ge=1, le=5, description="Relevance score: 1=useless 5=excellent")
    comment:   str | None = Field(None, max_length=500)


@router.post("/evaluation/rate")
async def rate_artifact(body: RateRequest, user=Depends(current_user)):
    """Submit a relevance rating for a proactive artifact."""
    try:
        result = submit_rating(
            user_id=user.id,
            file_path=body.file_path,
            rating=body.rating,
            comment=body.comment,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        log.exception("rate_artifact failed for user=%s", user.id)
        raise HTTPException(status_code=500, detail="rating submission failed")
    return result


@router.get("/evaluation/report")
async def evaluation_report(user=Depends(current_user)):
    """
    Return precision, relevance, and breakdown statistics for this user.
    This is the core research measurement endpoint.
    """
    return get_report(user.id)


@router.get("/evaluation/inferences")
async def list_inferences(
    limit: int = Query(default=20, ge=1, le=100),
    user=Depends(current_user),
):
    """List recent inference log entries — for debugging and research analysis."""
    with db._conn() as conn:
        rows = conn.execute(
            """SELECT id, inferred_goal, inferred_domain, confidence, source,
                      correct, proactive_artifact_id, ts
               FROM inference_log
               WHERE user_id = ?
               ORDER BY ts DESC LIMIT ?""",
            (user.id, limit),
        ).fetchall()
    return {
        "inferences": [dict(r) for r in rows],
        "count": len(rows),
    }
