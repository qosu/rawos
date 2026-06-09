"""
rawos Calendar Connector — Phase 4.

Polls CalDAV calendars for upcoming events.
Emits context_events of type 'calendar_event' for the proactive agent.
Runs as background asyncio task every 15 minutes.

Supported providers: Google Calendar, Apple iCloud, Fastmail, Nextcloud,
any CalDAV-compatible server.
"""
from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import json
import logging
import time
from typing import Any

import rawos.db as db
from rawos.config import settings

log = logging.getLogger("rawos.context.calendar")

_SYNC_INTERVAL_S          = 900    # 15 minutes between full syncs
_LOOKAHEAD_S              = 86400  # sync events up to 24h ahead
_NEEDS_ATTENTION_WINDOW_S = 7200   # fire attention trigger for events within 2h


# ---------------------------------------------------------------------------
# Credential encryption/decryption (Fernet — AES-128-CBC + HMAC-SHA256)
# Key derived from JWT secret via SHA-256 so we need no extra key management.
# ---------------------------------------------------------------------------

def _get_fernet():
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(
        hashlib.sha256(settings.jwt_secret.encode()).digest()
    )
    return Fernet(key)


def _encrypt_password(password: str) -> str:
    return _get_fernet().encrypt(password.encode()).decode()


def _decrypt_password(password_enc: str) -> str:
    return _get_fernet().decrypt(password_enc.encode()).decode()


# ---------------------------------------------------------------------------
# Credential management
# ---------------------------------------------------------------------------

def save_calendar_credentials(
    user_id: str,
    caldav_url: str,
    username: str,
    password: str,
) -> None:
    """Store encrypted CalDAV credentials for a user. Upserts on conflict."""
    password_enc = _encrypt_password(password)
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO calendar_credentials
                   (user_id, caldav_url, username, password_enc)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   caldav_url   = excluded.caldav_url,
                   username     = excluded.username,
                   password_enc = excluded.password_enc,
                   enabled      = 1,
                   sync_error   = NULL""",
            (user_id, caldav_url, username, password_enc),
        )


def remove_calendar_credentials(user_id: str) -> None:
    """Delete calendar credentials and events for a user."""
    with db._conn() as conn:
        conn.execute("DELETE FROM calendar_credentials WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM calendar_events WHERE user_id = ?", (user_id,))


def get_calendar_status(user_id: str) -> dict[str, Any] | None:
    """Return connection status dict (password excluded)."""
    with db._conn() as conn:
        row = conn.execute(
            """SELECT caldav_url, username, enabled, last_sync_ts, sync_error
               FROM calendar_credentials WHERE user_id = ?""",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    upcoming = len(get_upcoming_events(user_id, within_seconds=_LOOKAHEAD_S))
    return {
        "caldav_url":     row["caldav_url"],
        "username":       row["username"],
        "enabled":        bool(row["enabled"]),
        "last_sync_ts":   row["last_sync_ts"],
        "sync_error":     row["sync_error"],
        "upcoming_24h":   upcoming,
    }


# ---------------------------------------------------------------------------
# CalDAV sync
# ---------------------------------------------------------------------------

def sync_calendar_for_user(user_id: str) -> tuple[int, str | None]:
    """
    Sync upcoming events from user's CalDAV calendar.
    Returns (events_upserted, error_message_or_None).
    Gracefully handles missing caldav library.
    """
    try:
        import caldav
    except ImportError:
        return 0, "caldav library not installed"

    with db._conn() as conn:
        cred = conn.execute(
            """SELECT caldav_url, username, password_enc
               FROM calendar_credentials WHERE user_id = ? AND enabled = 1""",
            (user_id,),
        ).fetchone()

    if not cred:
        return 0, "no credentials configured"

    try:
        password = _decrypt_password(cred["password_enc"])
    except Exception as exc:
        return 0, f"credential decryption failed: {exc}"

    now_dt = datetime.datetime.now(datetime.timezone.utc)
    end_dt = now_dt + datetime.timedelta(seconds=_LOOKAHEAD_S)
    events_upserted = 0

    try:
        client = caldav.DAVClient(
            url=cred["caldav_url"],
            username=cred["username"],
            password=password,
        )
        principal = client.principal()
        calendars = principal.calendars()

        for calendar in calendars:
            try:
                cal_events = calendar.date_search(start=now_dt, end=end_dt, expand=True)
            except Exception:
                log.debug("date_search failed for calendar %s", calendar.url, exc_info=True)
                continue

            for cal_event in cal_events:
                upserted = _process_cal_event(user_id, cal_event, str(calendar.url or ""))
                if upserted:
                    events_upserted += 1

        with db._conn() as conn:
            conn.execute(
                "UPDATE calendar_credentials SET last_sync_ts = unixepoch(), sync_error = NULL WHERE user_id = ?",
                (user_id,),
            )
        log.debug("calendar synced %d events for user=%s", events_upserted, user_id)

    except Exception as exc:
        err = str(exc)[:200]
        log.warning("calendar sync failed user=%s: %s", user_id, err)
        try:
            with db._conn() as conn:
                conn.execute(
                    "UPDATE calendar_credentials SET sync_error = ? WHERE user_id = ?",
                    (err, user_id),
                )
        except Exception:
            pass
        return events_upserted, err

    return events_upserted, None


def _process_cal_event(user_id: str, cal_event: Any, calendar_url: str) -> bool:
    """
    Parse a caldav event, upsert into calendar_events, emit context event.
    Returns True if successfully processed.
    """
    try:
        vevent = cal_event.vobject_instance.vevent
        uid   = str(getattr(vevent, "uid", None) or "").strip()
        title = str(getattr(vevent, "summary", None) or "").strip()
        if not uid or not title:
            return False

        dtstart = getattr(vevent, "dtstart", None)
        dtend   = getattr(vevent, "dtend",   None)
        if not dtstart or not dtend:
            return False

        start_v, end_v = dtstart.value, dtend.value
        start_ts = _to_unix_ts(start_v)
        end_ts   = _to_unix_ts(end_v)
        if start_ts is None or end_ts is None:
            return False

        attendees_raw = getattr(vevent, "attendee", None)
        attendees: list[str] = []
        if attendees_raw is not None:
            items = (
                attendees_raw
                if hasattr(attendees_raw, "__iter__") and not isinstance(attendees_raw, str)
                else [attendees_raw]
            )
            attendees = [str(a).replace("mailto:", "").strip() for a in items if a]

        location    = str(getattr(vevent, "location",    None) or "").strip()
        description = str(getattr(vevent, "description", None) or "").strip()[:500]

        with db._conn() as conn:
            conn.execute(
                """INSERT INTO calendar_events
                       (user_id, external_id, title, start_ts, end_ts,
                        attendees, location, description, calendar_url, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, unixepoch())
                   ON CONFLICT(user_id, external_id) DO UPDATE SET
                       title        = excluded.title,
                       start_ts     = excluded.start_ts,
                       end_ts       = excluded.end_ts,
                       attendees    = excluded.attendees,
                       location     = excluded.location,
                       description  = excluded.description,
                       synced_at    = unixepoch()""",
                (user_id, uid, title, start_ts, end_ts,
                 json.dumps(attendees), location, description, calendar_url),
            )

        # Only emit a context event for events starting in the near future
        now = int(time.time())
        if start_ts >= now:
            _emit_calendar_context_event(user_id, uid, title, start_ts, end_ts, attendees)

        return True

    except Exception:
        log.debug("_process_cal_event failed", exc_info=True)
        return False


def _to_unix_ts(v: Any) -> int | None:
    """Convert datetime.date or datetime.datetime to Unix timestamp."""
    try:
        if isinstance(v, datetime.datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=datetime.timezone.utc)
            return int(v.timestamp())
        elif isinstance(v, datetime.date):
            return int(datetime.datetime(v.year, v.month, v.day,
                                         tzinfo=datetime.timezone.utc).timestamp())
    except Exception:
        pass
    return None


def _emit_calendar_context_event(
    user_id: str,
    uid: str,
    title: str,
    start_ts: int,
    end_ts: int,
    attendees: list[str],
) -> None:
    """
    Emit a calendar_event to context_events. Idempotent: at most once per uid per day.
    """
    now  = int(time.time())
    day  = now - (now % 86400)
    try:
        with db._conn() as conn:
            already = conn.execute(
                """SELECT 1 FROM context_events
                   WHERE user_id = ? AND event_type = 'calendar_event' AND ts >= ?
                     AND json_extract(metadata, '$.uid') = ?""",
                (user_id, day, uid),
            ).fetchone()
            if already:
                return
            conn.execute(
                """INSERT INTO context_events
                       (user_id, event_type, path, metadata, source_type)
                   VALUES (?, 'calendar_event', NULL, ?, 'calendar')""",
                (user_id, json.dumps({
                    "uid":       uid,
                    "title":     title,
                    "start_ts":  start_ts,
                    "end_ts":    end_ts,
                    "attendees": attendees,
                })),
            )
    except Exception:
        log.exception("_emit_calendar_context_event failed user=%s uid=%s", user_id, uid)


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def get_upcoming_events(
    user_id: str,
    within_seconds: int = _NEEDS_ATTENTION_WINDOW_S,
) -> list[dict[str, Any]]:
    """Return calendar events starting within the given window, sorted by start_ts."""
    now    = int(time.time())
    cutoff = now + within_seconds
    with db._conn() as conn:
        rows = conn.execute(
            """SELECT external_id, title, start_ts, end_ts, attendees, location
               FROM calendar_events
               WHERE user_id = ? AND start_ts >= ? AND start_ts <= ?
               ORDER BY start_ts ASC""",
            (user_id, now, cutoff),
        ).fetchall()
    result = []
    for r in rows:
        try:
            attendees = json.loads(r["attendees"] or "[]")
        except (json.JSONDecodeError, TypeError):
            attendees = []
        result.append({
            "uid":          r["external_id"],
            "title":        r["title"],
            "start_ts":     r["start_ts"],
            "end_ts":       r["end_ts"],
            "attendees":    attendees,
            "location":     r["location"] or "",
            "minutes_away": round((r["start_ts"] - now) / 60, 1),
        })
    return result


def has_attention_been_fired(user_id: str, event_uid: str) -> bool:
    """Return True if a NEEDS_ATTENTION artifact was already generated for this event today."""
    day = int(time.time())
    day -= day % 86400
    with db._conn() as conn:
        row = conn.execute(
            """SELECT 1 FROM proactive_artifacts
               WHERE user_id = ? AND action_type = 'attention'
                 AND created_at >= ?
                 AND goal LIKE ?""",
            (user_id, day, f"%{event_uid}%"),
        ).fetchone()
    return bool(row)


# ---------------------------------------------------------------------------
# Background sync loop
# ---------------------------------------------------------------------------

async def calendar_sync_loop() -> None:
    """Background task: sync all calendars every 15 minutes."""
    log.info("calendar sync loop started (interval=%ds)", _SYNC_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(_SYNC_INTERVAL_S)
            await _sync_all_async()
        except asyncio.CancelledError:
            log.info("calendar sync loop cancelled")
            break
        except Exception:
            log.exception("calendar sync loop error (continuing)")


async def _sync_all_async() -> None:
    """Sync calendars for all enabled users (offloaded to thread pool)."""
    try:
        with db._conn() as conn:
            rows = conn.execute(
                "SELECT user_id FROM calendar_credentials WHERE enabled = 1"
            ).fetchall()
    except Exception:
        log.exception("failed to query calendar_credentials")
        return

    loop = asyncio.get_event_loop()
    for row in rows:
        uid = row["user_id"]
        try:
            synced, err = await loop.run_in_executor(None, sync_calendar_for_user, uid)
            if err:
                log.warning("calendar sync user=%s error: %s", uid, err)
        except Exception:
            log.exception("calendar sync failed for user=%s", uid)
