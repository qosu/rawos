"""
rawos push notification service — Expo Push API.

Expo routes to FCM (Android) and APNs (iOS) without requiring a Firebase project.
API docs: https://docs.expo.dev/push-notifications/sending-notifications/

Rate limit: 600 req/s, batch up to 100 messages per call.
Error handling: DeviceNotRegistered → remove stale token from DB.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

import rawos.db as db

log = logging.getLogger("rawos.push")

_EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_EXPO_PUSH_RECEIPT_URL = "https://exp.host/--/api/v2/push/getReceipts"
_REQUEST_TIMEOUT_S = 10.0
_MAX_BATCH = 100


def _get_user_tokens(user_id: str) -> list[dict[str, str]]:
    """Return list of {id, expo_token} for user's registered devices."""
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT id, expo_token FROM push_devices WHERE user_id = ? ORDER BY last_active_at DESC",
            (user_id,),
        ).fetchall()
    return [{"id": r["id"], "expo_token": r["expo_token"]} for r in rows]


def _remove_stale_token(expo_token: str) -> None:
    """Remove a token that Expo reported as DeviceNotRegistered."""
    try:
        with db._conn() as conn:
            conn.execute("DELETE FROM push_devices WHERE expo_token = ?", (expo_token,))
        log.info("push: removed stale token %s", expo_token[:20])
    except Exception:
        log.exception("push: failed to remove stale token")


def _update_last_active(device_id: str) -> None:
    try:
        with db._conn() as conn:
            conn.execute(
                "UPDATE push_devices SET last_active_at = ? WHERE id = ?",
                (int(time.time()), device_id),
            )
    except Exception:
        pass


async def send_push_to_user(
    user_id: str,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> None:
    """
    Send push notification to all registered devices for user_id.
    Fire-and-forget safe — never raises, logs errors internally.
    Called via asyncio.create_task() from proactive scheduler.
    """
    devices = _get_user_tokens(user_id)
    if not devices:
        return

    messages = []
    device_map: dict[str, str] = {}  # expo_token → device_id
    for d in devices:
        messages.append({
            "to":    d["expo_token"],
            "title": title,
            "body":  body[:255],
            "data":  data or {},
            "sound": "default",
            "priority": "high",
        })
        device_map[d["expo_token"]] = d["id"]

    # Batch into chunks of _MAX_BATCH
    for i in range(0, len(messages), _MAX_BATCH):
        batch = messages[i : i + _MAX_BATCH]
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_S) as client:
                resp = await client.post(
                    _EXPO_PUSH_URL,
                    json=batch,
                    headers={
                        "Accept":       "application/json",
                        "Content-Type": "application/json",
                        "Accept-Encoding": "gzip, deflate",
                    },
                )
            if resp.status_code != 200:
                log.warning("push: Expo API returned %d: %s", resp.status_code, resp.text[:200])
                continue

            result = resp.json()
            tickets = result.get("data", [])
            for ticket, msg in zip(tickets, batch):
                if ticket.get("status") == "ok":
                    device_id = device_map.get(msg["to"])
                    if device_id:
                        _update_last_active(device_id)
                elif ticket.get("status") == "error":
                    details = ticket.get("details", {})
                    if details.get("error") == "DeviceNotRegistered":
                        _remove_stale_token(msg["to"])
                    else:
                        log.warning("push: ticket error for %s: %s", msg["to"][:20], ticket)

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("push: send_push_to_user failed (non-fatal)")
