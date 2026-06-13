"""rawos/scheduler/system_reflex.py — Phase 21: System FS Reflex.

Polls recent system_fs events from context_events (written by Phase 20 system_perception)
and triggers autonomous action via _run_proactive_agent when severity threshold is crossed
and path is not on cooldown.

Dormant by default (settings.system_fs_reflex_enabled = False).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import rawos.db as db
from rawos.config import settings
from rawos.inference.intent_engine import InferredIntent
from rawos.kernel.entity import RAWOS_ENTITY_USER_ID
from rawos.scheduler.proactive import _run_proactive_agent

log = logging.getLogger(__name__)

SYSTEM_FS_REFLEX_THRESHOLD: int = 5
SYSTEM_FS_REFLEX_COOLDOWN_S: int = 300


def _get_recent_system_fs_events(
    lookback_s: int,
    min_severity: int,
) -> list[dict[str, Any]]:
    cutoff = int(time.time()) - lookback_s
    with db._conn() as conn:
        rows = conn.execute(
            """SELECT path, event_type, metadata, ts
               FROM context_events
               WHERE user_id = ? AND source_type = 'system_fs' AND ts >= ?
               ORDER BY ts DESC""",
            (RAWOS_ENTITY_USER_ID, cutoff),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            meta = json.loads(row["metadata"] or "{}")
        except (ValueError, TypeError):
            meta = {}
        severity = int(meta.get("severity", 0))
        if severity < min_severity:
            continue
        result.append({
            "path": row["path"],
            "event_type": row["event_type"],
            "severity": severity,
            "ts": row["ts"],
        })
    return result


def _is_system_fs_cooldown(path: str) -> bool:
    cutoff = int(time.time()) - SYSTEM_FS_REFLEX_COOLDOWN_S
    with db._conn() as conn:
        row = conn.execute(
            """SELECT 1 FROM episodic_memory
               WHERE user_id = ? AND trigger_type = 'SYSTEM_FS_CHANGE'
               AND domain = ? AND ts >= ? LIMIT 1""",
            (RAWOS_ENTITY_USER_ID, path, cutoff),
        ).fetchone()
    return row is not None


async def _run_system_fs_reflex_scan() -> None:
    if not settings.system_fs_reflex_enabled:
        return

    events = _get_recent_system_fs_events(
        lookback_s=settings.system_fs_reflex_lookback_s,
        min_severity=SYSTEM_FS_REFLEX_THRESHOLD,
    )

    for event in events:
        path = event["path"]
        if _is_system_fs_cooldown(path):
            log.debug("system_fs_reflex: cooldown active path=%s", path)
            continue

        log.info(
            "rawos system_fs_reflex: event_type=%s severity=%d path=%s",
            event["event_type"], event["severity"], path,
        )

        intent_obj = InferredIntent(
            goal=f"System filesystem change detected: {event['event_type']} on {path}",
            domain=path,
            confidence=0.90,
            source="system_fs",
            suggested_actions=[
                f"investigate {event['event_type']}",
                "assess impact on rawos operation",
                "respond if action is needed",
            ],
        )

        await _run_proactive_agent(
            user_id=RAWOS_ENTITY_USER_ID,
            intent_obj=intent_obj,
            trigger_type="SYSTEM_FS_CHANGE",
            trigger_ctx={
                "path": path,
                "event_type": event["event_type"],
                "severity": event["severity"],
                "ts": event["ts"],
            },
            workdir_override=settings.rawos_source_root,
        )
        break  # one action per scan cycle — highest-priority first


async def system_fs_reflex_loop() -> None:
    if not settings.system_fs_reflex_enabled:
        log.debug("system_fs_reflex_loop: disabled, exiting")
        return

    log.info(
        "system_fs_reflex_loop: starting (interval=%ds threshold=%d cooldown=%ds)",
        settings.system_fs_reflex_interval_s,
        SYSTEM_FS_REFLEX_THRESHOLD,
        SYSTEM_FS_REFLEX_COOLDOWN_S,
    )
    while True:
        try:
            await _run_system_fs_reflex_scan()
        except Exception:
            log.exception("system_fs_reflex_scan failed")
        await asyncio.sleep(settings.system_fs_reflex_interval_s)
