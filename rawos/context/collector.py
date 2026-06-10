"""
rawos Context Collector.

Dual-source context collection:
  1. Filesystem watcher (watchdog inotify) — watches workspaces root for file changes
  2. DB poller — synthesizes semantic events from intents + artifacts + memories

Both sources write structured events to context_events table and update user_model.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

import rawos.db as db
from rawos.config import settings

log = logging.getLogger("rawos.context.collector")

# File types that carry semantic signal — ignore binaries, build artifacts
_SEMANTIC_EXTENSIONS = frozenset([
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".c", ".cpp", ".h",
    ".sql", ".md", ".txt", ".yaml", ".yml", ".toml", ".json", ".sh",
    ".html", ".css", ".env",
])
_IGNORE_PREFIXES = (".", "__pycache__", "node_modules", ".git", "dist", "build", ".next", "venv")
# rawos-generated artifact filenames — must not be treated as user activity (prevents feedback loop)
_IGNORE_FILE_PREFIXES = ("RAWOS_",)
_DOCUMENT_EXTENSIONS_SET = frozenset([".pdf", ".docx", ".doc"])


def _is_semantic(path: str) -> bool:
    p = Path(path)
    for part in p.parts:
        if any(part.startswith(pfx) for pfx in _IGNORE_PREFIXES):
            return False
    if any(p.name.startswith(pfx) for pfx in _IGNORE_FILE_PREFIXES):
        return False
    return p.suffix.lower() in _SEMANTIC_EXTENSIONS


def _is_document(path: str) -> bool:
    """Return True if path is a PDF/DOCX that rawos should extract text from."""
    p = Path(path)
    for part in p.parts:
        if any(part.startswith(pfx) for pfx in _IGNORE_PREFIXES):
            return False
    return p.suffix.lower() in _DOCUMENT_EXTENSIONS_SET


# ---------------------------------------------------------------------------
# Git + session perception utilities
# ---------------------------------------------------------------------------

# In-memory session state (per watchdog observer thread — no lock needed,
# watchdog fires events sequentially per observer).
_file_edit_times:    dict[str, list[float]] = {}  # filepath → recent edit timestamps
_last_commit_hash:   dict[str, str]         = {}  # repo_root → last seen HEAD hash
_last_git_diff_time: dict[str, float]       = {}  # filepath → last diff run timestamp

_SESSION_WINDOW_S = 1800  # 30 minutes — edits outside this window reset the count
_STUCK_THRESHOLD  = 5     # edits to same file within window → STUCK signal
_DIFF_DEBOUNCE_S  = 5     # minimum seconds between git diff calls per file

# Work session tracking (in-memory; survives restarts via DB)
_active_sessions: dict[str, tuple[str, int]] = {}  # user_id -> (session_id, last_ts)
_SESSION_IDLE_CLOSE_S = 900  # 15 minutes idle -> close session


def _detect_repo_root(file_path: str) -> str | None:
    """Walk up directory tree to find nearest .git directory.

    If file_path is itself a directory, check it before walking up —
    otherwise a repo root passed directly (e.g. server-scan affected_path)
    is skipped in favor of its parent.
    """
    resolved = Path(file_path).resolve()
    p = resolved if resolved.is_dir() else resolved.parent
    for _ in range(12):
        if (p / ".git").is_dir():
            return str(p)
        parent = p.parent
        if parent == p:
            break
        p = parent
    return None


def _get_git_context(repo_root: str, file_path: str) -> dict[str, Any]:
    """
    Return git diff summary + hunk for a file relative to HEAD.
    Debounced per file — returns empty dict if called too recently.
    All subprocess calls are timeout-guarded to prevent blocking the watcher thread.
    """
    now = time.time()
    if now - _last_git_diff_time.get(file_path, 0) < _DIFF_DEBOUNCE_S:
        return {}
    _last_git_diff_time[file_path] = now

    ctx: dict[str, Any] = {}
    try:
        rel = str(Path(file_path).relative_to(repo_root))
    except ValueError:
        return {}

    try:
        # Stat summary: "proactive.py | 47 +++---"
        r1 = subprocess.run(
            ["git", "-C", repo_root, "diff", "--stat", "HEAD", "--", rel],
            capture_output=True, text=True, timeout=2.0,
        )
        ctx["diff_summary"] = r1.stdout.strip()[:200] if r1.returncode == 0 else ""

        # Hunk: actual changed lines (truncated for DB storage)
        r2 = subprocess.run(
            ["git", "-C", repo_root, "diff", "--unified=3", "HEAD", "--", rel],
            capture_output=True, text=True, timeout=2.0,
        )
        ctx["diff_hunk"] = r2.stdout.strip()[:600] if r2.returncode == 0 else ""

        # If no uncommitted changes, show what the last commit changed for this file
        if not ctx["diff_summary"]:
            r3 = subprocess.run(
                ["git", "-C", repo_root, "diff", "--stat", "HEAD~1", "HEAD", "--", rel],
                capture_output=True, text=True, timeout=2.0,
            )
            ctx["diff_summary"] = r3.stdout.strip()[:200] if r3.returncode == 0 else ""

    except Exception:
        pass

    return ctx


def _update_session_edits(file_path: str) -> tuple[int, bool]:
    """
    Track edit count for a file within SESSION_WINDOW_S.
    Returns (edit_count_in_window, is_stuck).
    Thread-safe for single-threaded watchdog observer.
    """
    now = time.time()
    cutoff = now - _SESSION_WINDOW_S
    times = [t for t in _file_edit_times.get(file_path, []) if t > cutoff]
    times.append(now)
    _file_edit_times[file_path] = times
    count = len(times)
    return count, count >= _STUCK_THRESHOLD


def _check_emit_commit(repo_root: str, user_id: str) -> None:
    """
    Detect if HEAD changed since last check. If so, emit a git_commit context event.
    Called after each file_write event in a git repo.
    """
    try:
        r = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=1.0,
        )
        if r.returncode != 0:
            return
        current = r.stdout.strip()
        last = _last_commit_hash.get(repo_root)
        _last_commit_hash[repo_root] = current

        if last and last != current:
            # New commit — gather context
            r2 = subprocess.run(
                ["git", "-C", repo_root, "log", "--oneline", "-3"],
                capture_output=True, text=True, timeout=1.0,
            )
            r3 = subprocess.run(
                ["git", "-C", repo_root, "diff", "--stat", "HEAD~1", "HEAD"],
                capture_output=True, text=True, timeout=2.0,
            )
            _record_event(user_id, "git_commit", repo_root, {
                "commit_hash":    current[:12],
                "recent_commits": r2.stdout.strip()[:300] if r2.returncode == 0 else "",
                "files_changed":  r3.stdout.strip()[:400] if r3.returncode == 0 else "",
                "label":          "git_commit",
            })
            log.debug("git_commit event emitted for repo=%s user=%s", repo_root, user_id)
    except Exception:
        pass


def touch_work_session(user_id: str, stuck_signal: int = 0) -> str | None:
    """Open or update a work session for user. Close idle sessions. Returns current session_id."""
    now = int(time.time())
    session_id, last_ts = _active_sessions.get(user_id, (None, 0))

    if session_id and (now - last_ts) > _SESSION_IDLE_CLOSE_S:
        try:
            with db._conn() as conn:
                conn.execute(
                    "UPDATE work_sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
                    (last_ts, session_id),
                )
        except Exception:
            log.exception("failed to close idle work session %s", session_id)
        session_id = None

    if not session_id:
        try:
            with db._conn() as conn:
                row = conn.execute(
                    "INSERT INTO work_sessions (user_id, started_at, files_edited, stuck_signals) VALUES (?, ?, 1, ?) RETURNING id",
                    (user_id, now, stuck_signal),
                ).fetchone()
            session_id = row["id"] if row else None
        except Exception:
            log.exception("failed to open work session for user=%s", user_id)
            return None
    else:
        try:
            with db._conn() as conn:
                conn.execute(
                    "UPDATE work_sessions SET files_edited = files_edited + 1, stuck_signals = stuck_signals + ? WHERE id = ?",
                    (stuck_signal, session_id),
                )
        except Exception:
            log.exception("failed to update work session %s", session_id)

    if session_id:
        _active_sessions[user_id] = (session_id, now)
    return session_id


def increment_rawos_action(user_id: str) -> None:
    """Increment rawos_actions on user's current open work session."""
    session_id, _ = _active_sessions.get(user_id, (None, 0))
    if not session_id:
        return
    try:
        with db._conn() as conn:
            conn.execute(
                "UPDATE work_sessions SET rawos_actions = rawos_actions + 1 WHERE id = ?",
                (session_id,),
            )
    except Exception:
        log.exception("failed to increment rawos_actions for session %s", session_id)



def _user_id_from_workdir(path: str) -> str | None:
    """Extract user_id from workspace path: {workspaces_root}/{user_id}/{project_id}/..."""
    try:
        rel = Path(path).relative_to(settings.workspaces_root)
        parts = rel.parts
        if len(parts) >= 1:
            return parts[0]  # user_id is first path component
    except ValueError:
        pass
    return None


def _project_id_from_workdir(path: str) -> str | None:
    try:
        rel = Path(path).relative_to(settings.workspaces_root)
        parts = rel.parts
        if len(parts) >= 2:
            return parts[1]  # project_id is second component
    except ValueError:
        pass
    return None


def _record_event(user_id: str, event_type: str, path: str | None, metadata: dict[str, Any]) -> None:
    """
    Insert a context event. Extracts diff_summary, diff_hunk, session_edit_count,
    stuck_signal from metadata into dedicated columns for efficient querying.
    """
    diff_summary       = metadata.pop("diff_summary", None)
    diff_hunk          = metadata.pop("diff_hunk", None)
    session_edit_count = metadata.pop("session_edit_count", 1)
    stuck_signal       = metadata.pop("stuck_signal", 0)
    source_type        = metadata.pop("source_type", "file")
    try:
        with db._conn() as conn:
            conn.execute(
                """INSERT INTO context_events
                       (user_id, event_type, path, metadata,
                        diff_summary, diff_hunk, session_edit_count, stuck_signal, source_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, event_type, path, json.dumps(metadata),
                 diff_summary, diff_hunk, session_edit_count, stuck_signal, source_type),
            )
    except Exception:
        log.exception("failed to record context event user=%s type=%s", user_id, event_type)


class _WorkspaceHandler(FileSystemEventHandler):
    """Watchdog handler for workspace filesystem events."""

    def _handle(self, event: FileSystemEvent, event_type: str) -> None:
        if event.is_directory:
            return
        path = str(event.src_path)
        # Route document files to specialised handler (PDF/DOCX text extraction)
        if event_type == "file_write" and _is_document(path):
            self._handle_document(path)
            return
        if not _is_semantic(path):
            return
        user_id = _user_id_from_workdir(path)
        if not user_id:
            return
        project_id = _project_id_from_workdir(path)
        ext = Path(path).suffix.lower()
        _record_event(user_id, event_type, path, {
            "project_id": project_id,
            "extension": ext,
            "filename": Path(path).name,
        })

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle(event, "file_write")

    def on_modified(self, event: FileSystemEvent) -> None:
        self._handle(event, "file_write")

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._handle(event, "file_delete")


_observer: Observer | None = None


def start_filesystem_watcher() -> None:
    global _observer
    workspace_root = settings.workspaces_root
    if not Path(workspace_root).is_dir():
        log.warning("workspaces_root %s does not exist — filesystem watcher not started", workspace_root)
        return
    _observer = Observer()
    _observer.schedule(_WorkspaceHandler(), workspace_root, recursive=True)
    _observer.start()
    log.info("filesystem watcher started on %s", workspace_root)


def stop_filesystem_watcher() -> None:
    global _observer
    if _observer:
        _observer.stop()
        _observer.join(timeout=5)
        _observer = None
        log.info("filesystem watcher stopped")

# ---------------------------------------------------------------------------
# Personal filesystem watcher — watches arbitrary dirs from watched_paths table
# ---------------------------------------------------------------------------

_personal_observer: Observer | None = None
_personal_watches: dict[str, str] = {}  # path -> user_id (current state)


class _PersonalHandler(FileSystemEventHandler):
    """Watchdog handler for a personal workspace directory."""

    def __init__(self, user_id: str, base_path: str, label: str) -> None:
        self.user_id = user_id
        self.base_path = base_path
        self.label = label

    def _handle(self, event: FileSystemEvent, event_type: str) -> None:
        if event.is_directory:
            return
        path = str(event.src_path)
        if not _is_semantic(path):
            return
        ext = Path(path).suffix.lower()
        try:
            rel = str(Path(path).relative_to(self.base_path))
        except ValueError:
            rel = path

        metadata: dict[str, Any] = {
            "extension": ext,
            "filename":  Path(path).name,
            "rel_path":  rel,
            "label":     self.label,
        }

        if event_type == "file_write":
            # Session pattern: count edits, detect STUCK
            edit_count, is_stuck = _update_session_edits(path)
            metadata["session_edit_count"] = edit_count
            metadata["stuck_signal"]       = 1 if is_stuck else 0

            # Git context: diff summary + hunk (debounced, timeout-guarded)
            repo_root = _detect_repo_root(path)
            if repo_root:
                git_ctx = _get_git_context(repo_root, path)
                metadata.update(git_ctx)
                # Detect new commits while we're here
                _check_emit_commit(repo_root, self.user_id)

            # Work session: open/update, detect idle close
            touch_work_session(self.user_id, 1 if is_stuck else 0)

        _record_event(self.user_id, event_type, path, metadata)

    def on_created(self, event: FileSystemEvent) -> None:
        self._handle(event, "file_write")

    def on_modified(self, event: FileSystemEvent) -> None:
        self._handle(event, "file_write")

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._handle(event, "file_delete")

    def _handle_document(self, path: str) -> None:
        """Extract text from PDF/DOCX and emit document_change context event."""
        from rawos.context.documents import get_document_context
        doc_ctx = get_document_context(path)
        if not doc_ctx:
            return  # no change or extraction failed
        try:
            rel = str(Path(path).relative_to(self.base_path))
        except ValueError:
            rel = path
        metadata = {
            "extension": Path(path).suffix.lower(),
            "filename":  Path(path).name,
            "rel_path":  rel,
            "label":     self.label,
            **doc_ctx,
        }
        _record_event(self.user_id, "document_change", path, metadata)


def reload_personal_watcher() -> None:
    """
    Reload personal watcher from watched_paths table.
    Safe to call at any time — stops existing observer and starts a new one
    with current DB state. No-op if paths are unchanged.
    """
    global _personal_observer, _personal_watches

    try:
        with db._conn() as conn:
            rows = conn.execute(
                "SELECT user_id, path, label FROM watched_paths WHERE active = 1"
            ).fetchall()
    except Exception as exc:
        log.warning("reload_personal_watcher: DB read failed: %s", exc)
        return

    current: dict[str, tuple[str, str]] = {
        r["path"]: (r["user_id"], r["label"]) for r in rows
    }

    # Compare with current state (by path→user_id mapping)
    current_simple = {p: uid for p, (uid, _) in current.items()}
    if current_simple == _personal_watches:
        return  # No changes — skip reload

    # Stop existing personal observer
    if _personal_observer is not None:
        try:
            _personal_observer.stop()
            _personal_observer.join(timeout=3)
        except Exception:
            pass
        _personal_observer = None

    if not current:
        _personal_watches.clear()
        log.info("personal watcher stopped (no watched_paths registered)")
        return

    new_observer = Observer()
    started = 0
    for path, (user_id, label) in current.items():
        if Path(path).is_dir():
            new_observer.schedule(
                _PersonalHandler(user_id, path, label),
                path,
                recursive=True,
            )
            started += 1
        else:
            log.warning("watched_path %s does not exist — skipping", path)

    if started > 0:
        new_observer.start()
        _personal_observer = new_observer
        _personal_watches.clear()
        _personal_watches.update(current_simple)
        log.info("personal watcher reloaded: %d active paths", started)
    else:
        log.warning("personal watcher: no valid paths to watch")



async def record_intent_event(user_id: str, project_id: str, message: str) -> None:
    """Called by intent routes when an intent is submitted — enriches context."""
    _record_event(user_id, "intent_sent", None, {
        "project_id": project_id,
        "message_length": len(message),
        "message_preview": message[:120],
    })


async def record_artifact_event(user_id: str, project_id: str, artifact_name: str, artifact_type: str) -> None:
    """Called when an artifact is created — enriches context."""
    ext = Path(artifact_name).suffix.lower()
    _record_event(user_id, "artifact_created", artifact_name, {
        "project_id": project_id,
        "artifact_type": artifact_type,
        "extension": ext,
    })


async def db_sync_loop(interval_s: float = 30.0) -> None:
    """
    Background asyncio task: periodically synthesize DB state into context_events
    for users who had recent activity (last 5 minutes).

    This covers API activity that watchdog cannot see (remote users, Telegram adapter).
    """
    log.info("context db_sync_loop started (interval=%.0fs)", interval_s)
    while True:
        try:
            await asyncio.sleep(interval_s)
            _sync_db_activity()
        except asyncio.CancelledError:
            log.info("context db_sync_loop cancelled")
            break
        except Exception:
            log.exception("db_sync_loop error (continuing)")


def _sync_db_activity() -> None:
    """Pull recent intents + artifacts not yet reflected in context_events."""
    cutoff = int(time.time()) - 300  # last 5 minutes
    with db._conn() as conn:
        # Recent intents not yet in context_events
        rows = conn.execute(
            """SELECT i.user_id, i.project_id, i.raw_text, i.created_at
               FROM intents i
               WHERE i.created_at > ?
                 AND NOT EXISTS (
                     SELECT 1 FROM context_events ce
                     WHERE ce.user_id = i.user_id
                       AND ce.event_type = 'intent_sent'
                       AND ce.ts >= ?
                 )
               ORDER BY i.created_at DESC LIMIT 50""",
            (cutoff, cutoff),
        ).fetchall()
        for row in rows:
            _record_event(row["user_id"], "intent_sent", None, {
                "project_id": row["project_id"],
                "message_preview": (row["raw_text"] or "")[:120],
                "source": "db_sync",
            })

        # Recent artifacts
        rows = conn.execute(
            """SELECT a.user_id, a.project_id, a.name, a.type, a.created_at
               FROM artifacts a
               WHERE a.created_at > ?
                 AND NOT EXISTS (
                     SELECT 1 FROM context_events ce
                     WHERE ce.user_id = a.user_id
                       AND ce.event_type = 'artifact_created'
                       AND ce.ts >= ?
                 )
               ORDER BY a.created_at DESC LIMIT 50""",
            (cutoff, cutoff),
        ).fetchall()
        for row in rows:
            _record_event(row["user_id"], "artifact_created", row["name"], {
                "project_id": row["project_id"],
                "artifact_type": row["type"],
                "extension": Path(row["name"] or "").suffix.lower(),
                "source": "db_sync",
            })
