"""
anima User Model Aggregator.

Reads context_events for a user and synthesizes a semantic model:
  - current project
  - inferred tech stack
  - active domains (debugging / feature / research / etc.)
  - recent activity summary (last 20 events, semantically compressed)

Written to user_model table. Used by intent inference engine.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import anima.db as db

log = logging.getLogger("anima.context.user_model")

# Extension → stack tag
_EXT_STACK: dict[str, str] = {
    ".py": "python", ".pyx": "python", ".pyi": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".go": "go", ".rs": "rust", ".c": "c", ".cpp": "cpp",
    ".sql": "sql", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".json": "json", ".sh": "bash",
    ".html": "html", ".css": "css", ".md": "markdown",
}

# Keywords → domain tag
_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "debugging":    ["bug", "error", "fix", "debug", "trace", "exception", "crash", "fail"],
    "feature":      ["add", "implement", "build", "create", "new", "feature", "endpoint"],
    "refactor":     ["refactor", "clean", "rename", "move", "extract", "restructure"],
    "auth":         ["auth", "login", "token", "jwt", "session", "password", "oauth"],
    "data":         ["database", "schema", "migration", "query", "sql", "model", "table"],
    "api":          ["api", "endpoint", "route", "rest", "graphql", "request", "response"],
    "ui":           ["ui", "frontend", "component", "page", "style", "css", "layout"],
    "performance":  ["slow", "performance", "optimize", "cache", "latency", "speed"],
    "testing":      ["test", "spec", "mock", "assert", "pytest", "coverage"],
    "deployment":   ["deploy", "docker", "nginx", "server", "production", "service"],
}


def _infer_domains(texts: list[str]) -> list[str]:
    combined = " ".join(texts).lower()
    scores: dict[str, int] = {}
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        count = sum(combined.count(kw) for kw in keywords)
        if count > 0:
            scores[domain] = count
    return [d for d, _ in sorted(scores.items(), key=lambda x: -x[1])[:3]]


def _infer_stack(extensions: list[str]) -> list[str]:
    tags: list[str] = []
    for ext in extensions:
        tag = _EXT_STACK.get(ext)
        if tag and tag not in tags:
            tags.append(tag)
    return tags[:5]


def _most_recent_project(events: list[dict]) -> str | None:
    for ev in events:
        meta = ev.get("metadata", {})
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                continue
        pid = meta.get("project_id")
        if pid:
            return pid
    return None


def rebuild_user_model(user_id: str, lookback_s: int = 3600) -> dict[str, Any]:
    """
    Aggregate last `lookback_s` seconds of context_events for user_id
    and upsert into user_model. Returns the model dict.
    """
    cutoff = int(time.time()) - lookback_s
    with db._conn() as conn:
        rows = conn.execute(
            """SELECT event_type, path, metadata, ts
               FROM context_events
               WHERE user_id = ? AND ts >= ?
               ORDER BY ts DESC LIMIT 200""",
            (user_id, cutoff),
        ).fetchall()

    events = [dict(r) for r in rows]
    for ev in events:
        if isinstance(ev["metadata"], str):
            try:
                ev["metadata"] = json.loads(ev["metadata"])
            except Exception:
                ev["metadata"] = {}

    # Stack inference from file extensions
    extensions = []
    for ev in events:
        meta = ev["metadata"]
        ext = meta.get("extension", "")
        if ext:
            extensions.append(ext)
        path = ev.get("path") or ""
        if path:
            extensions.append(Path(path).suffix.lower())
    stack = _infer_stack(extensions)

    # Domain inference from intent messages
    texts = []
    for ev in events:
        if ev["event_type"] == "intent_sent":
            preview = ev["metadata"].get("message_preview", "")
            if preview:
                texts.append(preview)
    domains = _infer_domains(texts)

    # Most recent project
    project_id = _most_recent_project(events)

    # Recent activity summary (last 20, compressed)
    recent: list[dict] = []
    for ev in events[:20]:
        entry: dict[str, Any] = {"type": ev["event_type"], "ts": ev["ts"]}
        meta = ev["metadata"]
        if ev["event_type"] == "intent_sent":
            entry["preview"] = meta.get("message_preview", "")[:80]
        elif ev["event_type"] in ("file_write", "file_delete"):
            entry["file"] = meta.get("filename", "")
            entry["ext"] = meta.get("extension", "")
        elif ev["event_type"] == "artifact_created":
            entry["name"] = str(ev.get("path") or "")[:60]
        recent.append(entry)

    now = int(time.time())
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO user_model
               (user_id, current_project_id, inferred_stack, active_domains,
                recent_activity, goal_updated_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 current_project_id = COALESCE(excluded.current_project_id, user_model.current_project_id),
                 inferred_stack      = excluded.inferred_stack,
                 active_domains      = excluded.active_domains,
                 recent_activity     = excluded.recent_activity,
                 updated_at          = excluded.updated_at""",
            (
                user_id,
                project_id,
                json.dumps(stack),
                json.dumps(domains),
                json.dumps(recent),
                now,
                now,
            ),
        )

    # Episodic history (30-day lookback): anima cross-session understanding
    episodic_summary: list[dict] = []
    try:
        with db._conn() as conn:
            ep_rows = conn.execute(
                """SELECT trigger_type, domain, inferred_goal, decision,
                          action_summary, outcome, self_confidence, ts
                   FROM episodic_memory
                   WHERE user_id = ? AND ts > ?
                   ORDER BY ts DESC LIMIT 50""",
                (user_id, now - 2592000),  # 30 days
            ).fetchall()
        episodic_summary = [dict(r) for r in ep_rows]
    except Exception:
        pass  # table may not exist on first run; migration applies on startup

    return {
        "user_id": user_id,
        "current_project_id": project_id,
        "inferred_stack": stack,
        "active_domains": domains,
        "recent_activity": recent,
        "event_count": len(events),
        "episodic_history": episodic_summary,
    }


def get_user_model(user_id: str) -> dict[str, Any] | None:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT * FROM user_model WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row:
        return None
    m = dict(row)
    for field in ("inferred_stack", "active_domains", "recent_activity"):
        if isinstance(m.get(field), str):
            try:
                m[field] = json.loads(m[field])
            except Exception:
                m[field] = []
    return m
