"""anima/context/system_perception.py — Phase 20: Being's real-time filesystem senses.

Watches system-critical paths (anima source, /etc/anima*, systemd units) and persists
change events to context_events under RAWOS_ENTITY_USER_ID (source_type="system_fs").

Perception-only: events are never routed to agent action in this phase.
Dormant by default (system_perception_enabled=False).
Feedback guard: cage/worktree/workspaces paths are hard-excluded.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from anima.config import settings
from anima.context.collector import _IGNORE_PREFIXES, _record_event
from anima.kernel.entity import RAWOS_ENTITY_USER_ID

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Severity constants — no magic numbers (mirrors server_scanner.py convention)
# ---------------------------------------------------------------------------
SEVERITY_SOURCE_MODIFY: int = 3   # anima own source modified — notable
SEVERITY_SOURCE_DELETE: int = 6   # anima own source deleted — high
SEVERITY_CONFIG_MODIFY: int = 5   # /etc/anima* or systemd unit modified — high
SEVERITY_CONFIG_DELETE: int = 8   # config/systemd unit deleted — critical

# Event type names
EVENT_TYPE_SOURCE_CHANGE: str = "system_source_change"
EVENT_TYPE_CONFIG_CHANGE: str = "system_config_change"

# Transient suffixes generate noise without informational value
_TRANSIENT_SUFFIXES: tuple[str, ...] = (".swp", ".tmp", ".pyc", "~")


def _should_exclude(path: str) -> bool:
    """Return True if this path must NOT be persisted.

    Order matters: check cage/worktree FIRST (hard feedback guard),
    then workspaces (user-plane), then segment-based noise filters.
    """
    p = Path(path)

    # Cage/worktree exclusion — hard feedback guard.
    # Being's own self-modification writes here; sensing them would create a
    # perception → act → perception loop when Phase 21 reflex is wired.
    try:
        p.relative_to(settings.worktree_root)
        return True
    except ValueError:
        pass

    # User workspace exclusion — already watched by user-plane collector.py.
    try:
        p.relative_to(settings.workspaces_root)
        return True
    except ValueError:
        pass

    # Segment-based exclusion (mirrors collector._IGNORE_PREFIXES).
    for part in p.parts:
        if any(part.startswith(pfx) for pfx in _IGNORE_PREFIXES):
            return True
        # venv/.venv are not in _IGNORE_PREFIXES but must be excluded here
        if part in ("venv", ".venv", "node_modules", ".pytest_cache"):
            return True

    # Transient file extensions
    name = p.name
    if any(name.endswith(s) for s in _TRANSIENT_SUFFIXES):
        return True

    return False


def _classify(path: str, deleted: bool) -> tuple[str, int]:
    """Return (event_type, severity) based on path and operation.

    Paths under rawos_source_root → system_source_change (being senses itself changing).
    All other paths (config, systemd) → system_config_change (infrastructure changing).
    Deletion is always higher severity than modification.
    """
    p = Path(path)

    try:
        p.relative_to(settings.rawos_source_root)
        # Path is under anima source tree
        if deleted:
            return EVENT_TYPE_SOURCE_CHANGE, SEVERITY_SOURCE_DELETE
        return EVENT_TYPE_SOURCE_CHANGE, SEVERITY_SOURCE_MODIFY
    except ValueError:
        pass

    # Config/systemd or other watched path
    if deleted:
        return EVENT_TYPE_CONFIG_CHANGE, SEVERITY_CONFIG_DELETE
    return EVENT_TYPE_CONFIG_CHANGE, SEVERITY_CONFIG_MODIFY


class _SystemHandler(FileSystemEventHandler):
    """Watchdog handler for being-plane system perception.

    Called sequentially by the watchdog observer thread for each FS event.
    Applies exclusion filter, debounce/coalesce, then persists via _record_event.
    Errors in _record_event are caught and logged — never propagated.
    """

    def __init__(self, debounce_s: float) -> None:
        super().__init__()
        self._debounce_s = debounce_s
        # path → monotonic time of last recorded event
        self._debounce_cache: dict[str, float] = {}

    def _handle(self, path: str, deleted: bool) -> None:
        """Core dispatch: filter → debounce → classify → persist."""
        if _should_exclude(path):
            return

        now = time.monotonic()
        last = self._debounce_cache.get(path, 0.0)
        if now - last < self._debounce_s:
            return  # within debounce window — coalesce burst

        self._debounce_cache[path] = now

        event_type, severity = _classify(path, deleted)
        try:
            _record_event(
                RAWOS_ENTITY_USER_ID,
                event_type,
                path,
                {"source_type": "system_fs", "severity": severity},
            )
        except Exception:
            _log.exception(
                "system_perception: failed to record event path=%s type=%s",
                path, event_type,
            )

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(str(event.src_path), deleted=False)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(str(event.src_path), deleted=False)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(str(event.src_path), deleted=True)


# ---------------------------------------------------------------------------
# Module-level state (mirrors collector.py pattern)
# ---------------------------------------------------------------------------
_observer: Observer | None = None
_handler: _SystemHandler | None = None


def start_system_perception() -> None:
    """Start real-time filesystem perception for the being.

    Dormant by default (settings.system_perception_enabled=False).
    Call from app.py lifespan alongside start_filesystem_watcher().
    Paths that do not exist on disk are skipped with a warning.
    Already-running observer is a no-op (idempotent).
    """
    global _observer, _handler

    if not settings.system_perception_enabled:
        return

    if _observer is not None:
        return  # already running — idempotent

    debounce_s = settings.system_perception_debounce_s
    _handler = _SystemHandler(debounce_s=debounce_s)
    _observer = Observer()

    scheduled = 0
    for path_str in settings.system_perception_paths:
        p = Path(path_str)
        if p.is_dir():
            _observer.schedule(_handler, str(p), recursive=True)
            scheduled += 1
            _log.info("system_perception: watching %s (recursive)", p)
        else:
            _log.warning("system_perception: path not found, skipping: %s", p)

    if scheduled > 0:
        _observer.start()
        _log.info(
            "system_perception: observer started, %d path(s) watched, debounce=%.2fs",
            scheduled, debounce_s,
        )
    else:
        # No valid paths — do not leave a dangling observer
        _log.warning("system_perception: no valid watch paths found, observer not started")
        _observer = None
        _handler = None


def stop_system_perception() -> None:
    """Stop the system perception observer gracefully.

    Call from app.py lifespan shutdown alongside stop_filesystem_watcher().
    No-op if observer was never started.
    """
    global _observer, _handler
    if _observer is not None:
        _observer.stop()
        _observer.join()
        _observer = None
        _handler = None
        _log.info("system_perception: observer stopped")
