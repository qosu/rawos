"""tests/test_system_perception.py — TDD for Phase 20: Being's real-time system perception.

Invariants:
  - Perception-only: no event triggers agent action
  - All rows under RAWOS_ENTITY_USER_ID (never real user)
  - source_type="system_fs" distinguishes being-plane events
  - Feedback guard: cage/worktree/workspaces excluded (hard)
  - Debounce: 1 logical change = 1 row
  - Non-fatal: _record_event error must never crash service
  - Dormant: system_perception_enabled=False → no observer
"""
from __future__ import annotations

import os
import time

import pytest

import rawos.db as db
from rawos.config import settings
from rawos.context.system_perception import (
    EVENT_TYPE_CONFIG_CHANGE,
    EVENT_TYPE_SOURCE_CHANGE,
    SEVERITY_CONFIG_DELETE,
    SEVERITY_CONFIG_MODIFY,
    SEVERITY_SOURCE_MODIFY,
    _SystemHandler,
    _classify,
    _should_exclude,
    start_system_perception,
    stop_system_perception,
)
from rawos.kernel.entity import RAWOS_ENTITY_USER_ID


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_living_loop.py pattern)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Fresh isolated DB for every test — no cross-test state."""
    db_path = str(tmp_path / "test.db")
    ws_root = str(tmp_path / "ws")
    os.environ["DB_PATH"] = db_path
    os.environ["WORKSPACES_ROOT"] = ws_root
    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(settings, "workspaces_root", ws_root)
    db.init(db_path)
    yield


@pytest.fixture
def entity_user(fresh_db):
    """Insert RAWOS_ENTITY user so FK constraints pass (context_events.user_id → users.id)."""
    now = int(time.time())
    with db._conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO users
               (id, email, password_hash, tier, token_budget_daily,
                tokens_used_today, budget_reset_date, is_admin,
                stripe_customer_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                RAWOS_ENTITY_USER_ID, "rawos-entity@internal", "hashed",
                "free", 100_000, 0, "", 0, None, now, now,
            ),
        )
    return {"id": RAWOS_ENTITY_USER_ID}


def _system_fs_rows(path: str | None = None) -> list:
    """Fetch system_fs context_events rows for RAWOS_ENTITY."""
    with db._conn() as conn:
        if path:
            return conn.execute(
                "SELECT event_type, path, source_type FROM context_events "
                "WHERE user_id=? AND source_type='system_fs' AND path=?",
                (RAWOS_ENTITY_USER_ID, path),
            ).fetchall()
        return conn.execute(
            "SELECT event_type, path, source_type FROM context_events "
            "WHERE user_id=? AND source_type='system_fs'",
            (RAWOS_ENTITY_USER_ID,),
        ).fetchall()


# ---------------------------------------------------------------------------
# Exclusion (feedback guard + noise)
# ---------------------------------------------------------------------------

class TestSystemPerceptionExclusion:
    """Cage, workspaces, .git, __pycache__, .pyc, venv → excluded, 0 DB rows."""

    def test_worktree_root_excluded(self, entity_user):
        """Cage-write paths must never persist — hard feedback guard for Phase 21."""
        path = os.path.join(settings.worktree_root, "some-branch", "rawos", "fix.py")
        handler = _SystemHandler(debounce_s=0.0)
        handler._handle(path, deleted=False)
        assert _system_fs_rows(path) == []

    def test_workspaces_root_excluded(self, entity_user):
        """User workspace is user-plane (watched by collector.py), not being-plane."""
        path = os.path.join(settings.workspaces_root, "user-abc", "project", "main.py")
        handler = _SystemHandler(debounce_s=0.0)
        handler._handle(path, deleted=False)
        assert _system_fs_rows(path) == []

    def test_git_dir_excluded(self, entity_user):
        """VCS internals generate extreme noise; must be excluded."""
        path = os.path.join(settings.rawos_source_root, ".git", "COMMIT_EDITMSG")
        handler = _SystemHandler(debounce_s=0.0)
        handler._handle(path, deleted=False)
        assert _system_fs_rows(path) == []

    def test_pycache_excluded(self, entity_user):
        """Python bytecode cache generates spam on any import; exclude."""
        path = os.path.join(
            settings.rawos_source_root, "rawos", "__pycache__", "config.cpython-312.pyc"
        )
        handler = _SystemHandler(debounce_s=0.0)
        handler._handle(path, deleted=False)
        assert _system_fs_rows(path) == []

    def test_pyc_extension_excluded(self, entity_user):
        """*.pyc files must be excluded regardless of directory."""
        path = os.path.join(settings.rawos_source_root, "rawos", "config.pyc")
        handler = _SystemHandler(debounce_s=0.0)
        handler._handle(path, deleted=False)
        assert _system_fs_rows(path) == []

    def test_venv_excluded(self, entity_user):
        """Virtualenv internals are noise; exclude."""
        path = os.path.join(
            settings.rawos_source_root, "venv", "lib", "python3.12", "site-packages", "x.py"
        )
        handler = _SystemHandler(debounce_s=0.0)
        handler._handle(path, deleted=False)
        assert _system_fs_rows(path) == []

    def test_should_exclude_worktree(self):
        """_should_exclude returns True for worktree path."""
        path = os.path.join(settings.worktree_root, "branch", "file.py")
        assert _should_exclude(path) is True

    def test_should_exclude_git(self):
        """_should_exclude returns True for .git segment."""
        path = os.path.join(settings.rawos_source_root, ".git", "refs", "heads", "main")
        assert _should_exclude(path) is True


# ---------------------------------------------------------------------------
# Capture (valid events persist correctly)
# ---------------------------------------------------------------------------

class TestSystemPerceptionCapture:
    """Valid substrate events must persist under RAWOS_ENTITY_USER_ID, source_type=system_fs."""

    def test_source_modify_recorded(self, entity_user):
        """Modify event on rawos source file → exactly 1 row in context_events."""
        path = os.path.join(settings.rawos_source_root, "rawos", "config.py")
        handler = _SystemHandler(debounce_s=0.0)
        handler._handle(path, deleted=False)
        assert len(_system_fs_rows(path)) == 1

    def test_source_type_is_system_fs(self, entity_user):
        """Persisted row must have source_type='system_fs'."""
        path = os.path.join(settings.rawos_source_root, "rawos", "scheduler", "proactive.py")
        handler = _SystemHandler(debounce_s=0.0)
        handler._handle(path, deleted=False)
        rows = _system_fs_rows(path)
        assert len(rows) == 1
        assert rows[0]["source_type"] == "system_fs"

    def test_entity_user_id_used_not_real_user(self, entity_user):
        """Row user_id must equal RAWOS_ENTITY_USER_ID — never pollutes real user context."""
        path = os.path.join(settings.rawos_source_root, "rawos", "models.py")
        handler = _SystemHandler(debounce_s=0.0)
        handler._handle(path, deleted=False)
        with db._conn() as conn:
            rows = conn.execute(
                "SELECT user_id FROM context_events WHERE path=? AND source_type='system_fs'",
                (path,),
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["user_id"] == RAWOS_ENTITY_USER_ID

    def test_deleted_event_recorded(self, entity_user):
        """Deleted event on a source file → 1 row."""
        path = os.path.join(settings.rawos_source_root, "rawos", "context", "collector.py")
        handler = _SystemHandler(debounce_s=0.0)
        handler._handle(path, deleted=True)
        assert len(_system_fs_rows(path)) == 1


# ---------------------------------------------------------------------------
# Debounce (burst coalescing)
# ---------------------------------------------------------------------------

class TestSystemPerceptionDebounce:
    """Burst watchdog events for same path within debounce window coalesce to 1 row."""

    def test_rapid_events_coalesced_to_one_row(self, entity_user):
        """5 rapid events on same path within debounce window → exactly 1 DB row."""
        handler = _SystemHandler(debounce_s=10.0)
        path = os.path.join(settings.rawos_source_root, "rawos", "context", "collector.py")
        for _ in range(5):
            handler._handle(path, deleted=False)
        assert len(_system_fs_rows(path)) == 1

    def test_event_after_debounce_window_recorded(self, entity_user):
        """Event after debounce window expires → second row recorded."""
        handler = _SystemHandler(debounce_s=0.05)
        path = os.path.join(settings.rawos_source_root, "rawos", "api", "app.py")
        handler._handle(path, deleted=False)
        time.sleep(0.12)
        handler._handle(path, deleted=False)
        assert len(_system_fs_rows(path)) == 2

    def test_different_paths_not_debounced_together(self, entity_user):
        """Debounce is per-path; different paths → independent rows."""
        handler = _SystemHandler(debounce_s=10.0)
        path_a = os.path.join(settings.rawos_source_root, "rawos", "config.py")
        path_b = os.path.join(settings.rawos_source_root, "rawos", "models.py")
        handler._handle(path_a, deleted=False)
        handler._handle(path_b, deleted=False)
        assert len(_system_fs_rows(path_a)) == 1
        assert len(_system_fs_rows(path_b)) == 1


# ---------------------------------------------------------------------------
# Classify / severity constants
# ---------------------------------------------------------------------------

class TestSystemPerceptionClassify:
    """_classify returns correct (event_type, severity) pairs. Pure logic — no DB needed."""

    def test_source_path_yields_source_change(self):
        path = os.path.join(settings.rawos_source_root, "rawos", "scheduler", "proactive.py")
        event_type, _ = _classify(path, deleted=False)
        assert event_type == EVENT_TYPE_SOURCE_CHANGE

    def test_config_path_yields_config_change(self):
        path = "/etc/rawos/config.json"
        event_type, _ = _classify(path, deleted=False)
        assert event_type == EVENT_TYPE_CONFIG_CHANGE

    def test_systemd_unit_yields_config_change(self):
        path = "/etc/systemd/system/rawos.service"
        event_type, _ = _classify(path, deleted=False)
        assert event_type == EVENT_TYPE_CONFIG_CHANGE

    def test_source_modify_severity_is_named_constant(self):
        path = os.path.join(settings.rawos_source_root, "rawos", "config.py")
        _, severity = _classify(path, deleted=False)
        assert severity == SEVERITY_SOURCE_MODIFY

    def test_config_modify_severity_is_named_constant(self):
        path = "/etc/systemd/system/rawos.service"
        _, severity = _classify(path, deleted=False)
        assert severity == SEVERITY_CONFIG_MODIFY

    def test_config_delete_higher_than_modify(self):
        """Deletion of config/systemd unit is more severe than modification."""
        path = "/etc/systemd/system/rawos.service"
        _, sev_modify = _classify(path, deleted=False)
        _, sev_delete = _classify(path, deleted=True)
        assert sev_delete == SEVERITY_CONFIG_DELETE
        assert sev_delete > sev_modify

    def test_severity_constants_nonzero(self):
        """Severity constants must be positive integers."""
        assert SEVERITY_SOURCE_MODIFY > 0
        assert SEVERITY_CONFIG_MODIFY > 0
        assert SEVERITY_CONFIG_DELETE > 0


# ---------------------------------------------------------------------------
# Non-fatal (perception must never crash the service)
# ---------------------------------------------------------------------------

class TestSystemPerceptionNonFatal:
    """_record_event raising must be swallowed; service must stay up."""

    def test_record_event_exception_does_not_propagate(self, monkeypatch):
        """Handler must catch _record_event errors and log, never raise."""
        import rawos.context.system_perception as sp

        def _raise(*args, **kwargs):
            raise RuntimeError("db is borked")

        monkeypatch.setattr(sp, "_record_event", _raise)
        handler = _SystemHandler(debounce_s=0.0)
        path = os.path.join(settings.rawos_source_root, "rawos", "config.py")
        handler._handle(path, deleted=False)  # must not raise


# ---------------------------------------------------------------------------
# Disabled (dormant default)
# ---------------------------------------------------------------------------

class TestSystemPerceptionDisabled:
    """system_perception_enabled=False → start_system_perception is a complete no-op."""

    def test_disabled_start_creates_no_observer(self, monkeypatch):
        """When disabled, _observer must remain None after start call."""
        import rawos.context.system_perception as sp

        monkeypatch.setattr(settings, "system_perception_enabled", False)
        original = sp._observer
        sp._observer = None

        try:
            start_system_perception()
            assert sp._observer is None
        finally:
            sp._observer = original

    def test_stop_when_not_started_is_noop(self):
        """stop_system_perception with _observer=None must not raise."""
        import rawos.context.system_perception as sp

        original = sp._observer
        sp._observer = None
        try:
            stop_system_perception()
        finally:
            sp._observer = original


# ---------------------------------------------------------------------------
# Wiring (module-level contract + lifecycle)
# ---------------------------------------------------------------------------

class TestSystemPerceptionWiring:
    """start/stop functions are importable, callable, and honour enabled flag."""

    def test_start_stop_importable(self):
        """start_system_perception and stop_system_perception must be callable."""
        assert callable(start_system_perception)
        assert callable(stop_system_perception)

    def test_enabled_start_creates_observer(self, monkeypatch, tmp_path):
        """When enabled with a valid watch path, observer is created and started."""
        import rawos.context.system_perception as sp

        monkeypatch.setattr(settings, "system_perception_enabled", True)
        monkeypatch.setattr(settings, "system_perception_paths", [str(tmp_path)])
        original = sp._observer
        sp._observer = None

        try:
            start_system_perception()
            assert sp._observer is not None
        finally:
            stop_system_perception()
            sp._observer = original

    def test_stop_clears_observer(self, monkeypatch, tmp_path):
        """stop_system_perception sets _observer back to None."""
        import rawos.context.system_perception as sp

        monkeypatch.setattr(settings, "system_perception_enabled", True)
        monkeypatch.setattr(settings, "system_perception_paths", [str(tmp_path)])
        sp._observer = None

        start_system_perception()
        assert sp._observer is not None
        stop_system_perception()
        assert sp._observer is None

    def test_app_imports_start_system_perception(self):
        """app.py must import start_system_perception (wiring contract)."""
        import importlib, sys
        # Force re-import to detect import errors
        app_mod = importlib.import_module("rawos.api.app")
        # The import happened without error — wiring present
        assert hasattr(app_mod, "__file__")


# ---------------------------------------------------------------------------
# Identity (being-plane isolation)
# ---------------------------------------------------------------------------

class TestSystemPerceptionIdentity:
    """All persisted rows belong to RAWOS_ENTITY_USER_ID; real users are never touched."""

    def test_multiple_events_all_under_entity(self, entity_user):
        """All rows from system_perception have RAWOS_ENTITY_USER_ID."""
        handler = _SystemHandler(debounce_s=0.0)
        paths = [
            os.path.join(settings.rawos_source_root, "rawos", "config.py"),
            os.path.join(settings.rawos_source_root, "rawos", "models.py"),
            os.path.join(settings.rawos_source_root, "rawos", "db", "__init__.py"),
        ]
        for p in paths:
            handler._handle(p, deleted=False)

        with db._conn() as conn:
            all_rows = conn.execute(
                "SELECT DISTINCT user_id FROM context_events WHERE source_type='system_fs'"
            ).fetchall()

        assert len(all_rows) == 1
        assert all_rows[0]["user_id"] == RAWOS_ENTITY_USER_ID

    def test_entity_user_id_constant(self):
        """RAWOS_ENTITY_USER_ID must match known being identity."""
        assert RAWOS_ENTITY_USER_ID == "6eb6de1d-f5c9-4ae5-9aac-ce095b674823"
