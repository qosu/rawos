"""tests/test_system_reflex.py — TDD for Phase 21: System FS Reflex (event→action trigger).

Invariants:
  - All reflex triggers scoped to RAWOS_ENTITY_USER_ID only
  - Severity gate: only events >= SYSTEM_FS_REFLEX_THRESHOLD trigger action
  - Cooldown gate: path on cooldown → no duplicate trigger within SYSTEM_FS_REFLEX_COOLDOWN_S
  - One action per scan cycle (break after first)
  - trigger_type="SYSTEM_FS_CHANGE" on _run_proactive_agent call
  - workdir_override=settings.rawos_source_root
  - Dormant: system_fs_reflex_enabled=False → scan and loop are no-ops
  - Feedback guard: worktree_root excluded at Phase 20 perception layer; cooldown prevents
    re-trigger within cooldown window if being's own actions cause new system_fs events
"""
from __future__ import annotations

import asyncio
import json
import os
import time

import pytest

import rawos.db as db
import rawos.scheduler.system_reflex as system_reflex
from rawos.config import settings
from rawos.kernel.entity import RAWOS_ENTITY_USER_ID

THRESHOLD = system_reflex.SYSTEM_FS_REFLEX_THRESHOLD
COOLDOWN_S = system_reflex.SYSTEM_FS_REFLEX_COOLDOWN_S


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
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
    now = int(time.time())
    with db._conn() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO users
               (id, email, password_hash, tier, token_budget_daily,
                tokens_used_today, budget_reset_date, is_admin,
                stripe_customer_id, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (RAWOS_ENTITY_USER_ID, "rawos-entity@internal", "hashed",
             "free", 100_000, 0, "", 0, None, now, now),
        )
    return {"id": RAWOS_ENTITY_USER_ID}


def _insert_system_fs_event(
    path: str,
    event_type: str,
    severity: int,
    ts: int | None = None,
    user_id: str = RAWOS_ENTITY_USER_ID,
    source_type: str = "system_fs",
) -> None:
    if ts is None:
        ts = int(time.time()) - 5
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO context_events
               (user_id, event_type, path, metadata,
                diff_summary, diff_hunk, session_edit_count, stuck_signal, source_type, ts)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (user_id, event_type, path,
             json.dumps({"severity": severity}),
             None, None, 1, 0, source_type, ts),
        )


def _insert_episodic_reflex(path: str, ts: int | None = None) -> None:
    if ts is None:
        ts = int(time.time()) - 5
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO episodic_memory
               (user_id, trigger_type, domain, inferred_goal, decision, ts)
               VALUES (?,?,?,?,?,?)""",
            (RAWOS_ENTITY_USER_ID, "SYSTEM_FS_CHANGE", path,
             f"system change on {path}", "contribute", ts),
        )


# ---------------------------------------------------------------------------
# TestGetRecentSystemFsEvents
# ---------------------------------------------------------------------------

class TestGetRecentSystemFsEvents:
    def test_empty_when_no_events(self, entity_user):
        result = system_reflex._get_recent_system_fs_events(lookback_s=60, min_severity=THRESHOLD)
        assert result == []

    def test_returns_events_at_min_severity(self, entity_user):
        _insert_system_fs_event("/etc/rawos/config.json", "system_config_change", severity=THRESHOLD)
        result = system_reflex._get_recent_system_fs_events(lookback_s=60, min_severity=THRESHOLD)
        assert len(result) == 1
        assert result[0]["path"] == "/etc/rawos/config.json"

    def test_excludes_events_below_min_severity(self, entity_user):
        _insert_system_fs_event("/root/rawos/rawos/config.py", "system_source_change", severity=THRESHOLD - 1)
        result = system_reflex._get_recent_system_fs_events(lookback_s=60, min_severity=THRESHOLD)
        assert result == []

    def test_excludes_stale_events(self, entity_user):
        stale_ts = int(time.time()) - 200
        _insert_system_fs_event("/etc/rawos/config.json", "system_config_change", severity=8, ts=stale_ts)
        result = system_reflex._get_recent_system_fs_events(lookback_s=60, min_severity=THRESHOLD)
        assert result == []

    def test_excludes_non_system_fs_source_type(self, entity_user):
        _insert_system_fs_event(
            "/etc/rawos/config.json", "file_write", severity=9, source_type="file"
        )
        result = system_reflex._get_recent_system_fs_events(lookback_s=60, min_severity=THRESHOLD)
        assert result == []

    def test_result_contains_required_fields(self, entity_user):
        _insert_system_fs_event("/etc/rawos/config.json", "system_config_change", severity=7)
        result = system_reflex._get_recent_system_fs_events(lookback_s=60, min_severity=THRESHOLD)
        assert len(result) == 1
        row = result[0]
        assert "path" in row
        assert "event_type" in row
        assert "severity" in row
        assert "ts" in row
        assert row["severity"] == 7


# ---------------------------------------------------------------------------
# TestIsSystemFsCooldown
# ---------------------------------------------------------------------------

class TestIsSystemFsCooldown:
    def test_no_cooldown_initially(self, entity_user):
        assert system_reflex._is_system_fs_cooldown("/etc/rawos/config.json") is False

    def test_cooldown_after_episodic_record(self, entity_user):
        path = "/etc/rawos/config.json"
        _insert_episodic_reflex(path)
        assert system_reflex._is_system_fs_cooldown(path) is True

    def test_no_cooldown_after_expiry(self, entity_user):
        path = "/etc/rawos/config.json"
        stale_ts = int(time.time()) - COOLDOWN_S - 10
        _insert_episodic_reflex(path, ts=stale_ts)
        assert system_reflex._is_system_fs_cooldown(path) is False

    def test_cooldown_independent_per_path(self, entity_user):
        path_a = "/etc/rawos/config.json"
        path_b = "/etc/systemd/system/rawos.service"
        _insert_episodic_reflex(path_a)
        assert system_reflex._is_system_fs_cooldown(path_a) is True
        assert system_reflex._is_system_fs_cooldown(path_b) is False


# ---------------------------------------------------------------------------
# TestRunSystemFsReflexScan
# ---------------------------------------------------------------------------

class TestRunSystemFsReflexScan:
    async def test_no_op_when_disabled(self, entity_user, monkeypatch):
        monkeypatch.setattr(settings, "system_fs_reflex_enabled", False)
        _insert_system_fs_event("/etc/rawos/config.json", "system_config_change", severity=8)
        calls = []

        async def fake_agent(*args, **kw):
            calls.append(kw)

        monkeypatch.setattr(system_reflex, "_run_proactive_agent", fake_agent)
        await system_reflex._run_system_fs_reflex_scan()
        assert calls == []

    async def test_no_op_when_no_qualifying_events(self, entity_user, monkeypatch):
        monkeypatch.setattr(settings, "system_fs_reflex_enabled", True)
        calls = []

        async def fake_agent(*args, **kw):
            calls.append(kw)

        monkeypatch.setattr(system_reflex, "_run_proactive_agent", fake_agent)
        await system_reflex._run_system_fs_reflex_scan()
        assert calls == []

    async def test_triggers_on_threshold_severity_event(self, entity_user, monkeypatch):
        monkeypatch.setattr(settings, "system_fs_reflex_enabled", True)
        _insert_system_fs_event("/etc/rawos/config.json", "system_config_change", severity=THRESHOLD)
        calls = []

        async def fake_agent(*args, **kw):
            calls.append({"args": args, "kw": kw})

        monkeypatch.setattr(system_reflex, "_run_proactive_agent", fake_agent)
        await system_reflex._run_system_fs_reflex_scan()
        assert len(calls) == 1

    async def test_skips_cooled_down_path(self, entity_user, monkeypatch):
        monkeypatch.setattr(settings, "system_fs_reflex_enabled", True)
        path = "/etc/rawos/config.json"
        _insert_system_fs_event(path, "system_config_change", severity=8)
        _insert_episodic_reflex(path)
        calls = []

        async def fake_agent(*args, **kw):
            calls.append(kw)

        monkeypatch.setattr(system_reflex, "_run_proactive_agent", fake_agent)
        await system_reflex._run_system_fs_reflex_scan()
        assert calls == []

    async def test_one_action_per_scan_cycle(self, entity_user, monkeypatch):
        monkeypatch.setattr(settings, "system_fs_reflex_enabled", True)
        _insert_system_fs_event("/etc/rawos/a.json", "system_config_change", severity=8)
        _insert_system_fs_event("/etc/rawos/b.json", "system_config_change", severity=8)
        calls = []

        async def fake_agent(*args, **kw):
            calls.append(kw)

        monkeypatch.setattr(system_reflex, "_run_proactive_agent", fake_agent)
        await system_reflex._run_system_fs_reflex_scan()
        assert len(calls) == 1

    async def test_uses_rawos_entity_user_id(self, entity_user, monkeypatch):
        monkeypatch.setattr(settings, "system_fs_reflex_enabled", True)
        _insert_system_fs_event("/etc/rawos/config.json", "system_config_change", severity=6)
        calls = []

        async def fake_agent(*args, **kw):
            calls.append({"args": args, "kw": kw})

        monkeypatch.setattr(system_reflex, "_run_proactive_agent", fake_agent)
        await system_reflex._run_system_fs_reflex_scan()
        assert len(calls) == 1
        call = calls[0]
        uid = call["args"][0] if call["args"] else call["kw"].get("user_id")
        assert uid == RAWOS_ENTITY_USER_ID

    async def test_below_threshold_does_not_trigger(self, entity_user, monkeypatch):
        monkeypatch.setattr(settings, "system_fs_reflex_enabled", True)
        _insert_system_fs_event("/root/rawos/rawos/config.py", "system_source_change", severity=THRESHOLD - 1)
        calls = []

        async def fake_agent(*args, **kw):
            calls.append(kw)

        monkeypatch.setattr(system_reflex, "_run_proactive_agent", fake_agent)
        await system_reflex._run_system_fs_reflex_scan()
        assert calls == []


# ---------------------------------------------------------------------------
# TestSystemFsReflexTriggerContract
# ---------------------------------------------------------------------------

class TestSystemFsReflexTriggerContract:
    async def test_trigger_type_is_system_fs_change(self, entity_user, monkeypatch):
        monkeypatch.setattr(settings, "system_fs_reflex_enabled", True)
        _insert_system_fs_event("/etc/rawos/config.json", "system_config_change", severity=6)
        calls = []

        async def fake_agent(*args, **kw):
            calls.append(kw)

        monkeypatch.setattr(system_reflex, "_run_proactive_agent", fake_agent)
        await system_reflex._run_system_fs_reflex_scan()
        assert len(calls) == 1
        assert calls[0].get("trigger_type") == "SYSTEM_FS_CHANGE"

    async def test_workdir_override_is_rawos_source_root(self, entity_user, monkeypatch):
        monkeypatch.setattr(settings, "system_fs_reflex_enabled", True)
        _insert_system_fs_event("/etc/rawos/config.json", "system_config_change", severity=6)
        calls = []

        async def fake_agent(*args, **kw):
            calls.append(kw)

        monkeypatch.setattr(system_reflex, "_run_proactive_agent", fake_agent)
        await system_reflex._run_system_fs_reflex_scan()
        assert len(calls) == 1
        assert calls[0].get("workdir_override") == settings.rawos_source_root

    async def test_intent_source_is_system_fs(self, entity_user, monkeypatch):
        monkeypatch.setattr(settings, "system_fs_reflex_enabled", True)
        _insert_system_fs_event("/etc/rawos/config.json", "system_config_change", severity=6)
        calls = []

        async def fake_agent(*args, **kw):
            calls.append(kw)

        monkeypatch.setattr(system_reflex, "_run_proactive_agent", fake_agent)
        await system_reflex._run_system_fs_reflex_scan()
        assert len(calls) == 1
        intent = calls[0].get("intent_obj")
        assert intent is not None
        assert intent.source == "system_fs"

    async def test_trigger_ctx_contains_path_and_severity(self, entity_user, monkeypatch):
        monkeypatch.setattr(settings, "system_fs_reflex_enabled", True)
        path = "/etc/rawos/config.json"
        _insert_system_fs_event(path, "system_config_change", severity=7)
        calls = []

        async def fake_agent(*args, **kw):
            calls.append(kw)

        monkeypatch.setattr(system_reflex, "_run_proactive_agent", fake_agent)
        await system_reflex._run_system_fs_reflex_scan()
        assert len(calls) == 1
        ctx = calls[0].get("trigger_ctx", {})
        assert ctx.get("path") == path
        assert ctx.get("severity") == 7


# ---------------------------------------------------------------------------
# TestSystemFsReflexLoop
# ---------------------------------------------------------------------------

class TestSystemFsReflexLoop:
    async def test_exits_immediately_when_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "system_fs_reflex_enabled", False)
        await asyncio.wait_for(system_reflex.system_fs_reflex_loop(), timeout=1.0)

    async def test_loop_calls_scan_at_least_once(self, entity_user, monkeypatch):
        monkeypatch.setattr(settings, "system_fs_reflex_enabled", True)
        monkeypatch.setattr(settings, "system_fs_reflex_interval_s", 9999)
        scan_calls = []

        async def fake_scan():
            scan_calls.append(1)

        monkeypatch.setattr(system_reflex, "_run_system_fs_reflex_scan", fake_scan)
        task = asyncio.create_task(system_reflex.system_fs_reflex_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        assert len(scan_calls) >= 1
