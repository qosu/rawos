"""
rawos Timing API Routes — Phase 10.

Exposes timing signals and timeliness scores for research inspection.
GET /timing/signals  — full signal breakdown for a user
GET /timing/score    — scalar timeliness score + explanation
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from rawos.api.deps import current_user
from rawos.models import User

log = logging.getLogger("rawos.api.timing")
router = APIRouter(prefix="/timing")


@router.get("/signals")
def timing_signals(user: User = Depends(current_user)) -> dict:
    """Full timing signal breakdown for the authenticated user."""
    from rawos.timing.signals import compute_signals
    from rawos.context.user_model import get_user_model

    model = get_user_model(user.id)
    current_domain = (model.get("active_domains") or [None])[0]
    sig = compute_signals(user.id, current_domain=current_domain)
    return sig.to_dict()


@router.get("/score")
def timing_score(user: User = Depends(current_user)) -> dict:
    """Timeliness score with component breakdown and explanation."""
    from rawos.timing.model import get_timeliness, TIMELINESS_THRESHOLD
    from rawos.context.user_model import get_user_model

    model = get_user_model(user.id)
    current_domain = (model.get("active_domains") or [None])[0]
    result = get_timeliness(user.id, current_domain=current_domain)
    return {
        "threshold": TIMELINESS_THRESHOLD,
        "would_fire": result.timeliness_score >= TIMELINESS_THRESHOLD,
        **result.to_dict(),
    }
