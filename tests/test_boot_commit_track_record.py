"""tests/test_boot_commit_track_record.py — Stage 2 I-SR11: _self_reload_boot_commit_task
must update operator_track_record after resolving a self-reload outcome, and must pass the
autonomous flag from pending.json through to record_self_reload_outcome.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

import rawos.db as db_mod
import rawos.kernel.self_reload as sr_mod


def _write_pending(state_dir: Path, *, new_sha: str = "NEWSHA", autonomous: bool = False) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "pending.json").write_text(json.dumps({
        "old_sha": "OLDSHA",
        "new_sha": new_sha,
        "autonomous": autonomous,
        "state_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "armed_at": time.time(),
        "deadman_unit": "rawos-selfreload-revert",
    }))


def _run_task(monkeypatch, tmp_path: Path, *, outcome: str, autonomous: bool) -> tuple[list, list]:
    """Execute _self_reload_boot_commit_task with mocked boot_liveness_commit.

    Returns (record_calls, track_calls) where each entry is the kwargs dict
    from each respective db call.
    """
    pending_dir = tmp_path / "state"
    _write_pending(pending_dir, autonomous=autonomous)

    # Redirect state dir to our tmp location
    monkeypatch.setattr(sr_mod, "SELF_RELOAD_STATE_DIR", str(pending_dir))
    monkeypatch.setattr(sr_mod, "SELF_RELOAD_STATE_FILENAME", "pending.json")

    # Mock boot_liveness_commit to return a controlled outcome without doing anything real
    monkeypatch.setattr(sr_mod, "boot_liveness_commit", lambda **kw: outcome)

    # Capture db calls (db is imported at module level in app.py, so patch the module)
    record_calls: list[dict] = []
    track_calls: list[dict] = []

    _real_record = db_mod.record_self_reload_outcome
    _real_track = db_mod.update_operator_track_record

    monkeypatch.setattr(
        db_mod, "record_self_reload_outcome",
        lambda *a, **kw: record_calls.append({"args": a, "kw": kw}),
    )
    monkeypatch.setattr(
        db_mod, "update_operator_track_record",
        lambda *a, **kw: track_calls.append({"args": a, "kw": kw}),
    )

    # Import late so the monkeypatches above are in effect before local imports fire
    from rawos.api.app import _self_reload_boot_commit_task
    asyncio.run(_self_reload_boot_commit_task())

    return record_calls, track_calls


class TestBootCommitTaskTrackRecord:
    """I-SR11: boot_liveness_commit outcomes must update operator_track_record and pass autonomous."""

    def test_autonomous_true_passed_to_record(self, tmp_path, monkeypatch) -> None:
        record_calls, _ = _run_task(monkeypatch, tmp_path, outcome="committed", autonomous=True)
        assert len(record_calls) == 1
        assert record_calls[0]["kw"].get("autonomous") is True

    def test_autonomous_false_passed_to_record(self, tmp_path, monkeypatch) -> None:
        record_calls, _ = _run_task(monkeypatch, tmp_path, outcome="committed", autonomous=False)
        assert len(record_calls) == 1
        assert record_calls[0]["kw"].get("autonomous") is False

    def test_track_record_verified_true_on_committed(self, tmp_path, monkeypatch) -> None:
        _, track_calls = _run_task(monkeypatch, tmp_path, outcome="committed", autonomous=False)
        assert len(track_calls) == 1
        assert track_calls[0]["kw"]["verified"] is True

    def test_track_record_verified_false_on_resurrected(self, tmp_path, monkeypatch) -> None:
        _, track_calls = _run_task(monkeypatch, tmp_path, outcome="resurrected", autonomous=False)
        assert len(track_calls) == 1
        assert track_calls[0]["kw"]["verified"] is False

    def test_track_record_verified_false_on_liveness_failed(self, tmp_path, monkeypatch) -> None:
        _, track_calls = _run_task(monkeypatch, tmp_path, outcome="liveness_failed", autonomous=False)
        assert len(track_calls) == 1
        assert track_calls[0]["kw"]["verified"] is False

    def test_track_record_operation_class_is_self_reload(self, tmp_path, monkeypatch) -> None:
        _, track_calls = _run_task(monkeypatch, tmp_path, outcome="committed", autonomous=False)
        assert track_calls[0]["args"][1] == "self_reload"

    def test_no_track_record_when_no_pending(self, tmp_path, monkeypatch) -> None:
        """When state file does not exist, neither db function is called."""
        monkeypatch.setattr(sr_mod, "SELF_RELOAD_STATE_DIR", str(tmp_path / "empty"))
        monkeypatch.setattr(sr_mod, "SELF_RELOAD_STATE_FILENAME", "pending.json")

        record_calls: list = []
        track_calls: list = []
        monkeypatch.setattr(db_mod, "record_self_reload_outcome",
                           lambda *a, **kw: record_calls.append(kw))
        monkeypatch.setattr(db_mod, "update_operator_track_record",
                           lambda *a, **kw: track_calls.append(kw))

        from rawos.api.app import _self_reload_boot_commit_task
        asyncio.run(_self_reload_boot_commit_task())

        assert record_calls == []
        assert track_calls == []
