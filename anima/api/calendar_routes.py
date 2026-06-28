"""
anima Calendar API routes — Phase 4.

Endpoints:
  POST   /calendar/connect     — save CalDAV credentials + trigger immediate sync
  DELETE /calendar/disconnect  — delete credentials and calendar data
  GET    /calendar/status      — connection status + last sync info
  GET    /calendar/events      — upcoming events (next 24h)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from anima.api.deps import current_user
from anima.context.calendar import (
    get_calendar_status,
    get_upcoming_events,
    remove_calendar_credentials,
    save_calendar_credentials,
    sync_calendar_for_user,
)

router = APIRouter()
log = logging.getLogger("anima.api.calendar")


class _ConnectBody(BaseModel):
    caldav_url: str
    username:   str
    password:   str


@router.post("/connect")
async def calendar_connect(
    body: _ConnectBody,
    user=Depends(current_user),
) -> dict[str, Any]:
    """
    Save CalDAV credentials, test connection, and trigger immediate sync.
    Supports Google Calendar, Apple iCloud, Nextcloud, Fastmail, etc.
    """
    # Test connection before saving by attempting a sync
    save_calendar_credentials(
        user_id=user.id,
        caldav_url=body.caldav_url,
        username=body.username,
        password=body.password,
    )

    # Run sync in thread pool to avoid blocking event loop
    loop = asyncio.get_event_loop()
    synced, err = await loop.run_in_executor(
        None, sync_calendar_for_user, user.id
    )

    if err and synced == 0:
        # Connection failed completely — remove credentials so we don't store broken creds
        remove_calendar_credentials(user.id)
        raise HTTPException(
            status_code=400,
            detail=f"Calendar connection failed: {err}",
        )

    status = get_calendar_status(user.id)
    log.info("calendar connected: user=%s synced=%d", user.id, synced)
    return {
        "connected":      True,
        "events_synced":  synced,
        "sync_error":     err,
        "status":         status,
    }


@router.delete("/disconnect")
def calendar_disconnect(user=Depends(current_user)) -> dict[str, Any]:
    """Remove calendar credentials and all synced calendar data for this user."""
    status = get_calendar_status(user.id)
    if not status:
        raise HTTPException(status_code=404, detail="No calendar configured for this user")
    remove_calendar_credentials(user.id)
    log.info("calendar disconnected: user=%s", user.id)
    return {"disconnected": True}


@router.get("/status")
def calendar_status(user=Depends(current_user)) -> dict[str, Any]:
    """Return CalDAV connection status and upcoming event count."""
    status = get_calendar_status(user.id)
    if not status:
        return {"connected": False}
    return {"connected": True, **status}


@router.get("/events")
def calendar_events(
    user=Depends(current_user),
    hours: int = 24,
) -> dict[str, Any]:
    """Return upcoming calendar events within the next N hours (default 24)."""
    if hours < 1 or hours > 168:  # max 1 week
        raise HTTPException(status_code=400, detail="hours must be between 1 and 168")
    events = get_upcoming_events(user.id, within_seconds=hours * 3600)
    return {"events": events, "count": len(events)}
