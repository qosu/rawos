"""
rawos Trust Engine API routes — Phase 2.

Exposes autonomy track record and level management for the user.

Endpoints:
  GET  /trust/status  — current autonomy_grants levels + eligibility
  GET  /trust/history — recent proactive artifacts with rating + outcome
  POST /trust/grant   — upgrade action_type level (gated by good_count threshold)
  POST /trust/revoke  — reset action_type level to 0
"""
from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import rawos.db as db
from rawos.api.deps import current_user

router = APIRouter()
log = logging.getLogger("rawos.api.trust")

# Thresholds from PLAN.md — good_count required to reach each level
_UPGRADE_THRESHOLDS: dict[int, int] = {
    1: 10,
    2: 15,
    3: 20,
    4: 25,
}
_MAX_BAD_FOR_UPGRADE = 2  # bad_count must be < 2 to be eligible


@router.get("/status")
def trust_status(user=Depends(current_user)) -> dict[str, Any]:
    """Return autonomy_grants for user with upgrade eligibility per action_type."""
    with db._conn() as conn:
        rows = conn.execute(
            """SELECT action_type, level, good_count, bad_count, last_action, granted_at
               FROM autonomy_grants WHERE user_id = ? ORDER BY action_type""",
            (user.id,),
        ).fetchall()

    grants = []
    for r in rows:
        level = r["level"]
        good  = r["good_count"]
        bad   = r["bad_count"]
        next_threshold = _UPGRADE_THRESHOLDS.get(level + 1)
        eligible = (
            next_threshold is not None
            and good >= next_threshold
            and bad < _MAX_BAD_FOR_UPGRADE
        )
        grants.append({
            "action_type":         r["action_type"],
            "level":               level,
            "good_count":          good,
            "bad_count":           bad,
            "last_action":         r["last_action"],
            "granted_at":          r["granted_at"],
            "next_threshold":      next_threshold,
            "eligible_for_upgrade": eligible,
        })

    return {"grants": grants}


@router.get("/history")
def trust_history(user=Depends(current_user), limit: int = 20) -> dict[str, Any]:
    """Return recent proactive artifacts with their ratings and outcomes."""
    if limit > 100:
        limit = 100
    with db._conn() as conn:
        rows = conn.execute(
            """SELECT pa.file_path, pa.goal, pa.created_at, pa.action_type,
                      ar.rating, ar.comment, ar.ts AS rated_at
               FROM proactive_artifacts pa
               LEFT JOIN artifact_ratings ar
                   ON ar.proactive_artifact_id = pa.id AND ar.user_id = pa.user_id
               WHERE pa.user_id = ?
               ORDER BY pa.created_at DESC
               LIMIT ?""",
            (user.id, limit),
        ).fetchall()

    history = [
        {
            "file_path":   r["file_path"],
            "goal":        r["goal"],
            "created_at":  r["created_at"],
            "action_type": r["action_type"],
            "rating":      r["rating"],
            "comment":     r["comment"],
            "rated_at":    r["rated_at"],
            "outcome": (
                "good"    if r["rating"] and r["rating"] >= 3
                else "bad" if r["rating"]
                else "unrated"
            ),
        }
        for r in rows
    ]
    return {"history": history, "count": len(history)}


class _ActionTypeBody(BaseModel):
    action_type: str


# ---------------------------------------------------------------------------
# Tool access visibility endpoints (Phase B)
# ---------------------------------------------------------------------------

# Canonical map: tool → minimum autonomy level required.
# Ordered by level_required (ascending), then name — matches the trust status display.
_TOOL_LEVEL_MAP: list[dict] = [
    {"name": "bash_readonly", "level_required": 0,
     "description": "Read-only shell: cat, grep, ls, find, head, tail, wc, diff, git log/diff/status"},
    {"name": "list_files",    "level_required": 0,
     "description": "List files and directories in workdir"},
    {"name": "read_file",     "level_required": 0,
     "description": "Read any file in project workdir"},
    {"name": "write_file",    "level_required": 1,
     "description": "Create or overwrite files in project workdir"},
    {"name": "bash",          "level_required": 2,
     "description": "Full shell execution in workdir (30s timeout, path-isolated)"},
    {"name": "git_branch",    "level_required": 2,
     "description": "Create a rawos/* branch in the project repo (autonomous commits stay isolated)"},
    {"name": "git_commit",    "level_required": 2,
     "description": "Stage all changes and commit to current rawos/* branch (refuses main/master)"},
    {"name": "fetch_url",     "level_required": 3,
     "description": "Fetch content from external URLs (read-only)"},
    {"name": "deploy",        "level_required": 4,
     "description": "Publish project workspace to public URL"},
]



@router.get("/commits")
def trust_commits(
    user=Depends(current_user),
    limit: int = 30,
) -> dict[str, Any]:
    """Return all git commits rawos made autonomously, newest first."""
    if limit > 200:
        limit = 200
    with db._conn() as conn:
        rows = conn.execute(
            """SELECT rc.branch, rc.commit_hash, rc.message, rc.workdir, rc.created_at,
                      p.name AS project_name
               FROM rawos_commits rc
               LEFT JOIN projects p ON p.id = rc.project_id
               WHERE rc.user_id = ?
               ORDER BY rc.created_at DESC
               LIMIT ?""",
            (user.id, limit),
        ).fetchall()
    commits = [
        {
            "branch":       r["branch"],
            "commit_hash":  r["commit_hash"],
            "message":      r["message"],
            "workdir":      r["workdir"],
            "created_at":   r["created_at"],
            "project_name": r["project_name"],
        }
        for r in rows
    ]
    return {"commits": commits, "count": len(commits)}


@router.get("/tools/status")
def trust_tools_status(user=Depends(current_user)) -> dict[str, Any]:
    """Return all tools, the level required, and whether available at user's current trust level."""
    with db._conn() as conn:
        row = conn.execute(
            "SELECT level FROM autonomy_grants WHERE user_id = ? AND action_type = 'analysis'",
            (user.id,),
        ).fetchone()
    current_level = row["level"] if row else 0
    tools = [
        {**t, "available": t["level_required"] <= current_level}
        for t in _TOOL_LEVEL_MAP
    ]
    return {"current_level": current_level, "tools": tools}


@router.get("/tools/history")
def trust_tools_history(
    user=Depends(current_user),
    limit: int = 20,
) -> dict[str, Any]:
    """Return last N tool calls made autonomously by rawos proactive agent."""
    import json as _json
    if limit > 200:
        limit = 200
    with db._conn() as conn:
        rows = conn.execute(
            """SELECT ptc.tool_name, ptc.tool_input, ptc.tool_output,
                      ptc.success, ptc.duration_ms, ptc.called_at,
                      pa.file_path AS artifact_file
               FROM proactive_tool_calls ptc
               LEFT JOIN proactive_artifacts pa ON pa.id = ptc.artifact_id
               WHERE ptc.user_id = ?
               ORDER BY ptc.called_at DESC
               LIMIT ?""",
            (user.id, limit),
        ).fetchall()

    calls = []
    for r in rows:
        try:
            inp = _json.loads(r["tool_input"])
            preview = str(
                inp.get("command") or inp.get("path") or inp.get("url") or str(inp)
            )[:80]
        except Exception:
            preview = (r["tool_input"] or "")[:80]
        calls.append({
            "tool_name":     r["tool_name"],
            "input_preview": preview,
            "success":       bool(r["success"]),
            "duration_ms":   r["duration_ms"],
            "called_at":     r["called_at"],
            "artifact_file": r["artifact_file"],
            "output_size":   len(r["tool_output"] or ""),
        })
    return {"calls": calls, "count": len(calls)}


@router.post("/grant")
def trust_grant(body: _ActionTypeBody, user=Depends(current_user)) -> dict[str, Any]:
    """Upgrade autonomy level for action_type if track record qualifies."""
    now = int(time.time())

    with db._conn() as conn:
        row = conn.execute(
            "SELECT level, good_count, bad_count FROM autonomy_grants WHERE user_id = ? AND action_type = ?",
            (user.id, body.action_type),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=f"No trust record for action_type '{body.action_type}'")

    level      = row["level"]
    good_count = row["good_count"]
    bad_count  = row["bad_count"]
    threshold  = _UPGRADE_THRESHOLDS.get(level + 1)

    if threshold is None:
        raise HTTPException(status_code=400, detail=f"Level {level} is the maximum")
    if good_count < threshold:
        raise HTTPException(
            status_code=400,
            detail=f"Need {threshold} good ratings to reach level {level + 1}; current: {good_count}",
        )
    if bad_count >= _MAX_BAD_FOR_UPGRADE:
        raise HTTPException(
            status_code=400,
            detail=f"bad_count={bad_count} >= {_MAX_BAD_FOR_UPGRADE}; rate more artifacts positively first",
        )

    new_level = level + 1
    with db._conn() as conn:
        conn.execute(
            "UPDATE autonomy_grants SET level = ?, granted_at = ? WHERE user_id = ? AND action_type = ?",
            (new_level, now, user.id, body.action_type),
        )

    log.info("trust grant: user=%s action=%s level %d→%d", user.id, body.action_type, level, new_level)
    return {
        "action_type": body.action_type,
        "old_level":   level,
        "new_level":   new_level,
        "granted_at":  now,
    }


@router.post("/revoke")
def trust_revoke(body: _ActionTypeBody, user=Depends(current_user)) -> dict[str, Any]:
    """Immediately reset action_type autonomy level to 0."""
    with db._conn() as conn:
        row = conn.execute(
            "SELECT level FROM autonomy_grants WHERE user_id = ? AND action_type = ?",
            (user.id, body.action_type),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=f"No trust record for action_type '{body.action_type}'")

    old_level = row["level"]
    with db._conn() as conn:
        conn.execute(
            "UPDATE autonomy_grants SET level = 0, bad_count = 0 WHERE user_id = ? AND action_type = ?",
            (user.id, body.action_type),
        )

    log.info("trust revoke: user=%s action=%s level %d→0", user.id, body.action_type, old_level)
    return {"action_type": body.action_type, "old_level": old_level, "new_level": 0}
