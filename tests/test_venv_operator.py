"""
Tests for rawos.kernel.venv_operator — R-venv reversible dependency operator.

TDD contract:
  TestVenvPreflight     — preflight_venv: build + prove candidate in isolation
  TestArmAndSwapVenv    — arm_and_swap_venv: rename-swap + deadman + single-flight
  TestBootVenvCommit    — boot_venv_commit: liveness commit at startup
  TestVenvGate          — operate_on_venv + execute_approved_venv_op gate
  TestVenvAuditDb       — venv_operator_history ledger (needs migration 029)

Fake injection protocol (mirrors test_self_reload.py):
  FakeVenvBuilder  — avoids real python3 -m venv / pip / subprocess
  FakeVenvDeadman  — avoids real systemd-run
  FakeExit         — records _exit(0) without killing test process
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest

import rawos.db as db
from rawos.models import User

from rawos.kernel.venv_operator import (
    VENV_DEADMAN_UNIT,
    _VenvDeadmanSystemd,
    VENV_STATE_FILENAME,
    VenvDepSpec,
    VenvOperateOutcome,
    VenvPreflightError,
    VenvRefusalError,
    VenvSnapshot,
    VenvStateError,
    arm_and_swap_venv,
    boot_venv_commit,
    execute_approved_venv_op,
    operate_on_venv,
    preflight_venv,
)

# ─────────────────────────── fake helpers ────────────────────────────────────

class FakeVenvBuilder:
    """Injectable _builder: no real subprocess, controllable outcomes."""

    def __init__(
        self,
        *,
        install_ok: bool = True,
        import_ok: bool = True,
        smoke_ok: bool = True,
        freeze_str: str = "requests==2.31.0\n",
    ) -> None:
        self.install_ok = install_ok
        self.import_ok = import_ok
        self.smoke_ok = smoke_ok
        self.freeze_str = freeze_str
        self.calls: list[tuple] = []

    def frozen_hash(self, venv_python: str) -> str:
        self.calls.append(("frozen_hash", venv_python))
        return hashlib.sha256(self.freeze_str.encode()).hexdigest()

    def create_venv(self, path: str) -> None:
        self.calls.append(("create_venv", path))
        p = Path(path)
        p.mkdir(parents=True, exist_ok=True)
        (p / "bin").mkdir(exist_ok=True)
        (p / "bin" / "python").touch()

    def install_deps(
        self, venv_python: str, requirements: list[str]
    ) -> subprocess.CompletedProcess:
        self.calls.append(("install_deps", venv_python, requirements))
        rc = 0 if self.install_ok else 1
        err = "" if self.install_ok else "pip install error"
        return subprocess.CompletedProcess([], rc, "", err)

    def check_import(
        self, venv_python: str, module: str
    ) -> subprocess.CompletedProcess:
        self.calls.append(("check_import", venv_python, module))
        rc = 0 if self.import_ok else 1
        err = "" if self.import_ok else "ModuleNotFoundError: broken import"
        return subprocess.CompletedProcess([], rc, "", err)

    def run_smoke(
        self, venv_python: str, cwd: str
    ) -> subprocess.CompletedProcess:
        self.calls.append(("run_smoke", venv_python, cwd))
        rc = 0 if self.smoke_ok else 1
        out = "" if self.smoke_ok else "FAILED test_smoke.py::test_basic"
        return subprocess.CompletedProcess([], rc, out, "")


class FakeVenvDeadman:
    """Injectable _systemd: tracks arm/disarm without real systemd-run."""

    def __init__(self) -> None:
        self.arm_calls: list[tuple] = []
        self.disarm_calls: list[str] = []

    def arm(self, unit: str, delay_s: int, revert_cmd: str) -> None:
        self.arm_calls.append((unit, delay_s, revert_cmd))

    def disarm(self, unit: str) -> None:
        self.disarm_calls.append(unit)


class FakeExit:
    """Records _exit calls without killing the test process."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, code: int) -> None:
        self.calls.append(code)


def _make_layout(tmp_path: Path) -> tuple[Path, VenvSnapshot]:
    """Create fake old venv + candidate dirs. Return (venv_root, snapshot)."""
    state_id = "test-state-abcd1234"
    old_venv_id = f"old-{state_id}"
    venvs_dir = tmp_path / ".venvs"
    venvs_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "venv").mkdir()
    candidate_path = venvs_dir / f"candidate-{state_id}"
    candidate_path.mkdir()

    snap = VenvSnapshot(
        state_id=state_id,
        old_venv_id=old_venv_id,
        candidate_path=str(candidate_path),
        frozen_hash_before="before-hash",
        frozen_hash_after="after-hash",
        deadman_unit=VENV_DEADMAN_UNIT,
    )
    return tmp_path, snap


# ─────────────────────────── tests ───────────────────────────────────────────

class TestVenvPreflight:
    """I-VENV2: preflight isolates candidate, proves before any swap."""

    def test_import_ok_returns_snapshot(self, tmp_path: Path) -> None:
        snap = preflight_venv(
            VenvDepSpec(requirements=["requests"]),
            venv_root=str(tmp_path),
            staging_root=str(tmp_path),
            _builder=FakeVenvBuilder(),
            _source_root="/fake/rawos",
        )
        assert isinstance(snap, VenvSnapshot)
        assert snap.state_id != ""
        assert snap.candidate_path.endswith(f"candidate-{snap.state_id}")
        assert snap.frozen_hash_before != ""
        assert snap.frozen_hash_after != ""

    def test_install_fail_raises_preflight_error(self, tmp_path: Path) -> None:
        with pytest.raises(VenvPreflightError, match="pip install failed"):
            preflight_venv(
                VenvDepSpec(requirements=["broken-pkg"]),
                venv_root=str(tmp_path),
                staging_root=str(tmp_path),
                _builder=FakeVenvBuilder(install_ok=False),
                _source_root="/fake/rawos",
            )

    def test_import_fail_raises_preflight_error(self, tmp_path: Path) -> None:
        with pytest.raises(VenvPreflightError, match="import check failed"):
            preflight_venv(
                VenvDepSpec(requirements=[]),
                venv_root=str(tmp_path),
                staging_root=str(tmp_path),
                _builder=FakeVenvBuilder(import_ok=False),
                _source_root="/fake/rawos",
            )

    def test_smoke_fail_raises_preflight_error(self, tmp_path: Path) -> None:
        with pytest.raises(VenvPreflightError, match="venv_smoke"):
            preflight_venv(
                VenvDepSpec(requirements=[]),
                venv_root=str(tmp_path),
                staging_root=str(tmp_path),
                _builder=FakeVenvBuilder(smoke_ok=False),
                _source_root="/fake/rawos",
            )

    def test_frozen_hash_uses_sha256_of_pip_freeze(self, tmp_path: Path) -> None:
        freeze_str = "requests==2.31.0\nhttpx==0.27.0\n"
        builder = FakeVenvBuilder(freeze_str=freeze_str)
        expected = hashlib.sha256(freeze_str.encode()).hexdigest()
        snap = preflight_venv(
            VenvDepSpec(requirements=[]),
            venv_root=str(tmp_path),
            staging_root=str(tmp_path),
            _builder=builder,
            _source_root="/fake/rawos",
        )
        assert snap.frozen_hash_before == expected
        assert snap.frozen_hash_after == expected

    def test_candidate_cleaned_up_on_preflight_failure(self, tmp_path: Path) -> None:
        with pytest.raises(VenvPreflightError):
            preflight_venv(
                VenvDepSpec(requirements=[]),
                venv_root=str(tmp_path),
                staging_root=str(tmp_path),
                _builder=FakeVenvBuilder(import_ok=False),
                _source_root="/fake/rawos",
            )
        # No candidate dirs should remain
        venvs_dir = tmp_path / ".venvs"
        candidates = list(venvs_dir.glob("candidate-*")) if venvs_dir.exists() else []
        assert candidates == [], f"Candidate dir leaked: {candidates}"


class TestArmAndSwapVenv:
    """I-VENV1/I-VENV3/I-VENV4: rename-swap, deadman order, single-flight."""

    def test_state_written_before_arm(self, tmp_path: Path) -> None:
        venv_root, snap = _make_layout(tmp_path)
        state_dir = tmp_path / "state"
        sd = FakeVenvDeadman()
        state_written_at_arm: list[bool] = []

        original_arm = sd.arm

        def arm_capturing(*args):
            sf = state_dir / VENV_STATE_FILENAME
            state_written_at_arm.append(sf.exists())
            original_arm(*args)

        sd.arm = arm_capturing
        exit_fn = FakeExit()

        arm_and_swap_venv(
            snap,
            venv_root=str(venv_root),
            state_dir=str(state_dir),
            _systemd=sd,
            _exit=exit_fn,
        )
        assert state_written_at_arm == [True], "state.json must exist before arm()"

    def test_rename_swap_order_preserves_old(self, tmp_path: Path) -> None:
        """old venv → .venvs/old-<id>; candidate → venv."""
        venv_root, snap = _make_layout(tmp_path)
        sd = FakeVenvDeadman()
        exit_fn = FakeExit()

        arm_and_swap_venv(
            snap,
            venv_root=str(venv_root),
            state_dir=str(tmp_path / "state"),
            _systemd=sd,
            _exit=exit_fn,
        )

        old_dir = venv_root / ".venvs" / snap.old_venv_id
        assert old_dir.is_dir(), "old venv must be preserved as .venvs/old-<id>"
        assert (venv_root / "venv").is_dir(), "venv must exist (candidate was renamed here)"
        assert not Path(snap.candidate_path).exists(), "candidate must no longer exist at staging path"

    def test_exit_called_once_after_swap(self, tmp_path: Path) -> None:
        venv_root, snap = _make_layout(tmp_path)
        sd = FakeVenvDeadman()
        exit_fn = FakeExit()

        arm_and_swap_venv(
            snap,
            venv_root=str(venv_root),
            state_dir=str(tmp_path / "state"),
            _systemd=sd,
            _exit=exit_fn,
        )
        assert exit_fn.calls == [0]

    def test_disarm_and_no_exit_if_first_rename_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Rename fails → deadman disarmed, state cleared, exit NOT called."""
        venv_root, snap = _make_layout(tmp_path)
        state_dir = tmp_path / "state"
        sd = FakeVenvDeadman()
        exit_fn = FakeExit()

        import os as _os

        rename_count: list[int] = [0]
        original_rename = _os.rename

        def failing_rename(src, dst):
            rename_count[0] += 1
            if rename_count[0] == 1:
                raise OSError("simulated disk full")
            return original_rename(src, dst)

        monkeypatch.setattr(_os, "rename", failing_rename)

        with pytest.raises(OSError, match="simulated disk full"):
            arm_and_swap_venv(
                snap,
                venv_root=str(venv_root),
                state_dir=str(state_dir),
                _systemd=sd,
                _exit=exit_fn,
            )

        assert len(sd.disarm_calls) >= 1, "deadman must be disarmed on swap failure"
        assert exit_fn.calls == [], "_exit must NOT be called when swap failed"
        assert not (state_dir / VENV_STATE_FILENAME).exists(), "state must be cleared on failure"

    def test_single_flight_refuses_second(self, tmp_path: Path) -> None:
        venv_root, snap = _make_layout(tmp_path)
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / VENV_STATE_FILENAME).write_text(
            json.dumps({"state_id": "other-pending"})
        )
        sd = FakeVenvDeadman()
        exit_fn = FakeExit()

        with pytest.raises(VenvStateError, match="pending"):
            arm_and_swap_venv(
                snap,
                venv_root=str(venv_root),
                state_dir=str(state_dir),
                _systemd=sd,
                _exit=exit_fn,
            )

    def test_state_json_contains_required_keys(self, tmp_path: Path) -> None:
        venv_root, snap = _make_layout(tmp_path)
        state_dir = tmp_path / "state"
        sd = FakeVenvDeadman()
        exit_fn = FakeExit()

        arm_and_swap_venv(
            snap,
            venv_root=str(venv_root),
            state_dir=str(state_dir),
            _systemd=sd,
            _exit=exit_fn,
        )

        data = json.loads((state_dir / VENV_STATE_FILENAME).read_text())
        assert data["state_id"] == snap.state_id
        assert data["old_venv_id"] == snap.old_venv_id
        assert data["deadman_unit"] == VENV_DEADMAN_UNIT
        assert "armed_at" in data


class TestVenvDeadmanSystemd:
    """I-VENV3: real systemd-run arm + disarm — disarm must stop the .timer unit.

    Regression for a bug found via twin-prove: disarm() called
    `systemctl stop <unit>` (defaults to .service), leaving the transient
    `.timer` unit active forever. Must stop `<unit>.timer`.
    """

    def test_disarm_stops_timer_unit(self) -> None:
        sd = _VenvDeadmanSystemd()
        unit = "rawos-venv-revert-pytest"
        sd.arm(unit, 3600, "/bin/true")
        try:
            timers = subprocess.run(
                ["systemctl", "list-timers", "--all"], capture_output=True, text=True
            ).stdout
            assert f"{unit}.timer" in timers, "arm() did not create timer unit"

            sd.disarm(unit)

            timers_after = subprocess.run(
                ["systemctl", "list-timers", "--all"], capture_output=True, text=True
            ).stdout
            assert f"{unit}.timer" not in timers_after, "disarm() did not stop timer unit"
        finally:
            subprocess.run(
                ["systemctl", "stop", f"{unit}.timer", f"{unit}.service"],
                capture_output=True,
            )


class TestBootVenvCommit:
    """I-VENV3: boot disarms deadman on health, leaves armed on crash-loop."""

    def _write_state(self, state_dir: Path, snap: VenvSnapshot) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / VENV_STATE_FILENAME).write_text(
            json.dumps(
                {
                    "state_id": snap.state_id,
                    "old_venv_id": snap.old_venv_id,
                    "candidate_path": snap.candidate_path,
                    "frozen_hash_before": snap.frozen_hash_before,
                    "frozen_hash_after": snap.frozen_hash_after,
                    "armed_at": 0.0,
                    "deadman_unit": snap.deadman_unit,
                }
            )
        )

    def test_no_pending_returns_no_pending(self, tmp_path: Path) -> None:
        result = boot_venv_commit(
            venv_root=str(tmp_path),
            state_dir=str(tmp_path / "state"),
            staging_root=str(tmp_path),
            _probe=lambda: True,
            _systemd=FakeVenvDeadman(),
        )
        assert result == "no_pending"

    def test_healthy_returns_committed_disarms_clears(self, tmp_path: Path) -> None:
        venv_root = tmp_path
        state_dir = tmp_path / "state"
        snap = VenvSnapshot(
            state_id="commit-id-001",
            old_venv_id="old-commit-id-001",
            candidate_path="irrelevant",
            frozen_hash_before="bef",
            frozen_hash_after="aft",
            deadman_unit=VENV_DEADMAN_UNIT,
        )
        self._write_state(state_dir, snap)
        # Create old dir to be reaped
        old_dir = venv_root / ".venvs" / snap.old_venv_id
        old_dir.mkdir(parents=True)

        sd = FakeVenvDeadman()
        result = boot_venv_commit(
            venv_root=str(venv_root),
            state_dir=str(state_dir),
            staging_root=str(venv_root),
            _probe=lambda: True,
            _systemd=sd,
        )

        assert result == "committed"
        assert sd.disarm_calls == [VENV_DEADMAN_UNIT], "must disarm on healthy boot"
        assert not (state_dir / VENV_STATE_FILENAME).exists(), "state must be cleared"
        assert not old_dir.exists(), "old venv must be reaped on committed"

    def test_unhealthy_leaves_armed_returns_liveness_failed(
        self, tmp_path: Path
    ) -> None:
        venv_root = tmp_path
        state_dir = tmp_path / "state"
        snap = VenvSnapshot(
            state_id="fail-id-002",
            old_venv_id="old-fail-id-002",
            candidate_path="irr",
            frozen_hash_before="b",
            frozen_hash_after="a",
            deadman_unit=VENV_DEADMAN_UNIT,
        )
        self._write_state(state_dir, snap)

        sd = FakeVenvDeadman()
        result = boot_venv_commit(
            venv_root=str(venv_root),
            state_dir=str(state_dir),
            staging_root=str(venv_root),
            _probe=lambda: False,
            _systemd=sd,
        )

        assert result == "liveness_failed"
        assert sd.disarm_calls == [], "must NOT disarm deadman when unhealthy"
        assert (
            state_dir / VENV_STATE_FILENAME
        ).exists(), "state must remain on disk when deadman still armed"


class TestVenvGate:
    """I-VENV5: gate proposes when dormant, auto-applies when enabled+graduated."""

    @pytest.fixture(autouse=True)
    def _db(self, tmp_path: Path) -> None:
        db.init(str(tmp_path / "gate_test.db"))

    def _seed_graduation(self, user_id: str, venv_root: str) -> None:
        import time as _time
        from rawos.kernel.track_record import GRADUATION_THRESHOLD

        for _ in range(GRADUATION_THRESHOLD * 2):
            db.update_operator_track_record(
                user_id,
                "venv_dep_update",
                venv_root,
                verified=True,
                now=int(_time.time()),
            )

    def test_propose_when_flag_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import rawos.config as _cfg

        monkeypatch.setattr(_cfg.settings, "operator_venv_enabled", False)
        user = db.create_user(User(email="venv-gate-a@test.com", password_hash="x"))
        outcome = operate_on_venv(
            user.id,
            VenvDepSpec(requirements=[]),
            _venv_root=str(tmp_path),
            _staging_root=str(tmp_path),
            _builder=FakeVenvBuilder(),
        )
        assert not outcome.auto_applied
        assert outcome.proposed

    def test_propose_when_not_graduated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import rawos.config as _cfg

        monkeypatch.setattr(_cfg.settings, "operator_venv_enabled", True)
        user = db.create_user(User(email="venv-gate-b@test.com", password_hash="x"))
        outcome = operate_on_venv(
            user.id,
            VenvDepSpec(requirements=[]),
            _venv_root=str(tmp_path),
            _staging_root=str(tmp_path),
            _builder=FakeVenvBuilder(),
        )
        assert not outcome.auto_applied
        assert outcome.proposed

    def test_auto_applies_when_enabled_and_graduated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import rawos.config as _cfg

        monkeypatch.setattr(_cfg.settings, "operator_venv_enabled", True)
        # Set up dirs for rename-swap
        (tmp_path / "venv").mkdir()
        (tmp_path / ".venvs").mkdir(parents=True)

        user = db.create_user(User(email="venv-gate-c@test.com", password_hash="x"))
        self._seed_graduation(user.id, str(tmp_path))

        exit_fn = FakeExit()
        sd = FakeVenvDeadman()

        outcome = operate_on_venv(
            user.id,
            VenvDepSpec(requirements=[]),
            _venv_root=str(tmp_path),
            _staging_root=str(tmp_path),
            _builder=FakeVenvBuilder(),
            _systemd=sd,
            _exit=exit_fn,
            _state_dir=str(tmp_path / "state"),
        )

        assert outcome.auto_applied, "must auto-apply when flag=True and graduated"
        assert not outcome.proposed
        assert exit_fn.calls == [0], "swap exit must have been called"

    def test_execute_approved_bypasses_flag_and_graduation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """execute_approved_venv_op bypasses flag + graduation, keeps preflight."""
        import rawos.config as _cfg

        monkeypatch.setattr(_cfg.settings, "operator_venv_enabled", False)
        (tmp_path / "venv").mkdir()
        (tmp_path / ".venvs").mkdir(parents=True)

        user = db.create_user(User(email="venv-gate-d@test.com", password_hash="x"))
        exit_fn = FakeExit()
        sd = FakeVenvDeadman()

        outcome = execute_approved_venv_op(
            user.id,
            VenvDepSpec(requirements=[]),
            _venv_root=str(tmp_path),
            _staging_root=str(tmp_path),
            _builder=FakeVenvBuilder(),
            _systemd=sd,
            _exit=exit_fn,
            _state_dir=str(tmp_path / "state"),
        )

        assert outcome.auto_applied, "owner-path must apply regardless of flag+graduation"
        assert exit_fn.calls == [0]

    def test_execute_approved_still_runs_preflight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """owner-path bypass does NOT bypass preflight (I-VENV2 unbypassable)."""
        import rawos.config as _cfg

        monkeypatch.setattr(_cfg.settings, "operator_venv_enabled", False)
        user = db.create_user(User(email="venv-gate-e@test.com", password_hash="x"))
        exit_fn = FakeExit()

        with pytest.raises(VenvPreflightError):
            execute_approved_venv_op(
                user.id,
                VenvDepSpec(requirements=[]),
                _venv_root=str(tmp_path),
                _staging_root=str(tmp_path),
                _builder=FakeVenvBuilder(import_ok=False),
                _systemd=FakeVenvDeadman(),
                _exit=exit_fn,
                _state_dir=str(tmp_path / "state"),
            )

        assert exit_fn.calls == [], "exit must NOT be called when preflight fails"


class TestVenvAuditDb:
    """I-VENV7: venv_operator_history ledger."""

    @pytest.fixture(autouse=True)
    def _db(self, tmp_path: Path) -> None:
        db.init(str(tmp_path / "audit_test.db"))

    def test_record_and_retrieve(self) -> None:
        from rawos.db import record_venv_op_outcome, list_venv_op_history

        record_venv_op_outcome(
            op_type="dep_update",
            frozen_hash_before="abc",
            frozen_hash_after="def",
            outcome="applied",
        )
        rows = list_venv_op_history(limit=1)
        assert len(rows) == 1
        assert rows[0]["op_type"] == "dep_update"
        assert rows[0]["frozen_hash_before"] == "abc"
        assert rows[0]["frozen_hash_after"] == "def"
        assert rows[0]["outcome"] == "applied"
        assert rows[0]["autonomous"] == 0

    def test_autonomous_flag_stored(self) -> None:
        from rawos.db import record_venv_op_outcome, list_venv_op_history

        record_venv_op_outcome(
            op_type="dep_update",
            frozen_hash_before="x",
            frozen_hash_after="y",
            outcome="applied",
            autonomous=True,
        )
        rows = list_venv_op_history(limit=1)
        assert rows[0]["autonomous"] == 1

    def test_multiple_rows_newest_first(self) -> None:
        from rawos.db import record_venv_op_outcome, list_venv_op_history

        record_venv_op_outcome("dep_update", "h1", "h2", "proposed")
        record_venv_op_outcome("dep_update", "h3", "h4", "applied")
        rows = list_venv_op_history(limit=10)
        # newest (h3→h4 applied) must come first
        applied = [r for r in rows if r["frozen_hash_before"] == "h3"]
        proposed = [r for r in rows if r["frozen_hash_before"] == "h1"]
        assert applied and proposed
        assert rows.index(applied[0]) < rows.index(proposed[0])

    def test_outcome_check_constraint(self) -> None:
        from rawos.db import record_venv_op_outcome
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            record_venv_op_outcome("dep_update", "x", "y", "invalid_outcome")
