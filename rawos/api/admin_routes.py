"""rawos Admin API — Phase 5. All endpoints require is_admin=True."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import rawos.db as db
from rawos.api.deps import current_user
from rawos.models import User

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="admin access required")
    return user


class AdminUserRow(BaseModel):
    id:                 str
    email:              str
    tier:               str
    is_admin:           bool
    tokens_used_today:  int
    token_budget_daily: int
    created_at:         int


class AdminStats(BaseModel):
    users_total:   int
    intents_today: int
    tokens_today:  int
    errors_today:  int
    active_agents: int


@router.get("/stats", response_model=AdminStats)
async def get_stats(admin: User = Depends(_require_admin)) -> AdminStats:
    stats = db.get_admin_stats()
    return AdminStats(**stats)


@router.get("/users", response_model=list[AdminUserRow])
async def list_users(
    limit: int = 100,
    admin: User = Depends(_require_admin),
) -> list[AdminUserRow]:
    users = db.get_all_users(limit=limit)
    return [
        AdminUserRow(
            id=u.id, email=u.email, tier=u.tier.value,
            is_admin=u.is_admin,
            tokens_used_today=u.tokens_used_today,
            token_budget_daily=u.token_budget_daily,
            created_at=u.created_at,
        )
        for u in users
    ]


@router.post("/users/{user_id}/set-admin")
async def set_admin_flag(
    user_id: str,
    is_admin: bool,
    admin: User = Depends(_require_admin),
) -> dict:
    db.set_admin(user_id, is_admin)
    return {"ok": True, "user_id": user_id, "is_admin": is_admin}


@router.get("/errors")
async def get_recent_errors(
    limit: int = 50,
    admin: User = Depends(_require_admin),
) -> list[dict]:
    """Return recent error events from the events log."""
    return db.get_recent_errors(limit=limit)
