"""tests/test_self_reload.py — TDD for rawos/kernel/self_reload.py (Phase 25 Stage 1).

TDD Iron Law: this file must go RED before self_reload.py is written.

Mirrors tests/test_pam_operator.py's injectable-fake structure:
  _runner   — FakeRunner (avoids real git/subprocess)
  _worktree — FakeWorktree (avoids real `git worktree add`)
  _systemd  — FakeSelfReloadDeadman (avoids real systemd-run)
  _exit     — recording fake (NEVER call real os._exit in tests)
  _now/_sleep — deterministic clock control for boot_liveness_commit retries

old_sha is always "OLDSHA" (via the `git rev-parse HEAD` fake response);
new_sha is always "NEWSHA" unless a test says otherwise.
"""
from __future__ import annotations

import sys

import hashlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

import pytest

import rawos.db as db
from rawos.kernel.self_reload import (
    SELF_RELOAD_DEADMAN_DELAY_S,
    SELF_RELOAD_DEADMAN_UNIT,
    SELF_RELOAD_STATE_DIR,
    SelfReloadOperateOutcome,
    SelfReloadPreflightError,
    SelfReloadRefusalError,
    SelfReloadSnapshot,
    SelfReloadStateError,
    arm_and_swap,
    boot_liveness_commit,
    execute_owner_self_reload,
    operate_on_self_reload,
    preflight_stage,
)
from rawos.kernel.track_record import GRADUATION_THRESHOLD
from rawos.models import User


# ---------------------------------------------------------------------------
# Config-driven state dir (mirrors SOURCE_ROOT = settings.rawos_source_root)
# ---------------------------------------------------------------------------

class TestConfigDrivenStateDir:
    """SELF_RELOAD_STATE_DIR must be config-driven, mirroring
    SOURCE_ROOT = settings.rawos_source_root -- so a twin-prove process (its
    own .env, its own settings instance) gets its own state dir without
    per-call _state_dir injection, and both arm_and_swap and the automatic
    boot-commit task (app.py::_self_reload_boot_commit_task, which never
    passes _state_dir) agree on the same path."""

    def test_state_dir_reflects_settings(self) -> None:
        from rawos.config import settings

        assert SELF_RELOAD_STATE_DIR == settings.self_reload_state_dir
        assert settings.self_reload_state_dir == "/root/.rawos-selfreload"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """Maps exact (args-tuple) -> FakeResult. Unmatched calls succeed with empty output."""

    def __init__(self, responses: dict | None = None, order: list | None = None,
                 order_tag: str | None = None) -> None:
        self.responses = dict(responses or {})
        self.calls: list[tuple] = []
        self._order = order
        self._order_tag = order_tag

    def run(self, args: list[str], cwd: str) -> FakeResult:
        key = tuple(args)
        self.calls.append((key, cwd))
        if self._order is not None and key[:3] == ("git", "reset", "--hard"):
            self._order.append(self._order_tag or "swap")
        return self.responses.get(key, FakeResult(returncode=0, stdout="", stderr=""))


class FakeWorktree:
    def __init__(self, path: str = "/fake/worktree") -> None:
        self.path = path
        self.created: list[tuple[str, str]] = []
        self.removed: list[str] = []

    def create(self, repo_path: str, sha: str) -> str:
        self.created.append((repo_path, sha))
        return self.path

    def remove(self, worktree_path: str) -> None:
        self.removed.append(worktree_path)


class TestSelfReloadDeadmanSystemd:
    """I-SR3 (mirror I-VENV3): real systemd-run arm + disarm against
    _SelfReloadDeadmanSystemd -- disarm() must stop the `.timer` unit, not
    the bare unit name (regression class found via venv twin-prove).
    Twin unit name only; never touches rawos-selfreload-revert."""

    def test_disarm_stops_timer_unit(self) -> None:
        import subprocess

        from rawos.kernel.self_reload import _SelfReloadDeadmanSystemd

        sd = _SelfReloadDeadmanSystemd()
        unit = "rawos-selfreload-revert-pytest"
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


class FakeSelfReloadDeadman:
    def __init__(self, order: list | None = None, on_arm=None) -> None:
        self.armed: list[tuple[str, int, str]] = []
        self.disarmed: list[str] = []
        self._order = order
        self._on_arm = on_arm

    def arm(self, unit: str, delay_s: int, revert_cmd: str) -> None:
        if self._on_arm:
            self._on_arm()
        if self._order is not None:
            self._order.append("arm")
        self.armed.append((unit, delay_s, revert_cmd))

    def disarm(self, unit: str) -> None:
        self.disarmed.append(unit)


class FakeExit:
    """Records os._exit() calls without killing the test process."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def __call__(self, code: int) -> None:
        self.calls.append(code)


_REV_PARSE_HEAD = ("git", "rev-parse", "HEAD")


def _runner(extra: dict | None = None, order: list | None = None, order_tag=None) -> FakeRunner:
    base = {_REV_PARSE_HEAD: FakeResult(stdout="OLDSHA\n")}
    if extra:
        base.update(extra)
    return FakeRunner(base, order=order, order_tag=order_tag)


def _snapshot(**overrides) -> SelfReloadSnapshot:
    base = dict(
        old_sha="OLDSHA",
        new_sha="NEWSHA",
        state_id=str(uuid.uuid4()),
        armed_at=0.0,
        deadman_unit=SELF_RELOAD_DEADMAN_UNIT,
        migration_delta=[],
        venv_frozen_hash="hash",
    )
    base.update(overrides)
    return SelfReloadSnapshot(**base)


# ---------------------------------------------------------------------------
# preflight_stage() — refusals
# ---------------------------------------------------------------------------

class TestPreflightRefusals:
    def test_refuses_migration_add(self, tmp_path: Path) -> None:
        runner = _runner({
            ("git", "diff", "--name-only", "OLDSHA..NEWSHA", "--", "migrations/"):
                FakeResult(stdout="migrations/026_managed_self_reload.sql\n"),
        })
        with pytest.raises(SelfReloadRefusalError):
            preflight_stage("NEWSHA", _source_root="/fake/repo", _runner=runner, _worktree=FakeWorktree())

    def test_refuses_migration_rename_or_delete(self, tmp_path: Path) -> None:
        runner = _runner({
            ("git", "diff", "--name-only", "OLDSHA..NEWSHA", "--", "migrations/"):
                FakeResult(stdout="migrations/025_managed_pam_targets.sql\n"),
        })
        with pytest.raises(SelfReloadRefusalError):
            preflight_stage("NEWSHA", _source_root="/fake/repo", _runner=runner, _worktree=FakeWorktree())

    def test_refuses_venv_drift(self, tmp_path: Path) -> None:
        runner = _runner({
            ("git", "show", "NEWSHA:pyproject.toml"): FakeResult(stdout="new dependency added\n"),
        })
        with pytest.raises(SelfReloadRefusalError, match="dependenc"):
            preflight_stage("NEWSHA", _source_root="/fake/repo", _runner=runner, _worktree=FakeWorktree())

    def test_refuses_tier0_violation(self, tmp_path: Path) -> None:
        runner = _runner({
            ("git", "diff", "--name-only", "OLDSHA..NEWSHA"):
                FakeResult(stdout="rawos/kernel/operator.py\n"),
        })
        with pytest.raises(SelfReloadRefusalError, match="TIER-0"):
            preflight_stage("NEWSHA", _source_root="/fake/repo", _runner=runner, _worktree=FakeWorktree())

    def test_refuses_on_import_error(self, tmp_path: Path) -> None:
        runner = _runner({
            (sys.executable, "-c", "import rawos.api.app"):
                FakeResult(returncode=1, stderr="ModuleNotFoundError: no module named 'rawos'"),
        })
        with pytest.raises(SelfReloadPreflightError, match="import"):
            preflight_stage("NEWSHA", _source_root="/fake/repo", _runner=runner, _worktree=FakeWorktree())

    def test_refuses_on_test_subset_failure(self, tmp_path: Path) -> None:
        runner = _runner({
            (sys.executable, "-m", "pytest", "-q", "-m", "self_reload_smoke"):
                FakeResult(returncode=1, stdout="1 failed"),
        })
        with pytest.raises(SelfReloadPreflightError, match="smoke"):
            preflight_stage("NEWSHA", _source_root="/fake/repo", _runner=runner, _worktree=FakeWorktree())

    def test_success_returns_snapshot_without_arming(self, tmp_path: Path) -> None:
        runner = _runner()
        worktree = FakeWorktree()
        snap = preflight_stage("NEWSHA", _source_root="/fake/repo", _runner=runner, _worktree=worktree)

        assert snap.old_sha == "OLDSHA"
        assert snap.new_sha == "NEWSHA"
        assert snap.migration_delta == []
        assert snap.deadman_unit == SELF_RELOAD_DEADMAN_UNIT
        assert snap.armed_at == 0.0
        uuid.UUID(snap.state_id)  # valid uuid, raises ValueError otherwise

        # worktree was used and cleaned up — no armed/swap side effects yet
        assert worktree.created == [("/fake/repo", "NEWSHA")]
        assert worktree.removed == [worktree.path]


# ---------------------------------------------------------------------------
# arm_and_swap() — ordering invariants (I-SR2, I-SR8)
# ---------------------------------------------------------------------------

class TestArmAndSwap:
    def test_state_written_before_arm(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "selfreload-state"
        state_path = state_dir / "pending.json"
        seen_exists_at_arm = []

        sd = FakeSelfReloadDeadman(on_arm=lambda: seen_exists_at_arm.append(state_path.exists()))
        runner = _runner()
        exit_fn = FakeExit()

        arm_and_swap(
            _snapshot(), _systemd=sd, _exit=exit_fn,
            _source_root="/fake/repo", _runner=runner, _state_dir=str(state_dir),
        )

        assert seen_exists_at_arm == [True]
        record = json.loads(state_path.read_text())
        assert record["old_sha"] == "OLDSHA"
        assert record["new_sha"] == "NEWSHA"

    def test_arm_before_swap(self, tmp_path: Path) -> None:
        order: list[str] = []
        sd = FakeSelfReloadDeadman(order=order)
        runner = _runner(order=order, order_tag="swap")
        exit_fn = FakeExit()

        arm_and_swap(
            _snapshot(), _systemd=sd, _exit=exit_fn,
            _source_root="/fake/repo", _runner=runner, _state_dir=str(tmp_path / "state"),
        )

        assert order == ["arm", "swap"]

    def test_swap_then_exit_once(self, tmp_path: Path) -> None:
        sd = FakeSelfReloadDeadman()
        runner = _runner()
        exit_fn = FakeExit()

        arm_and_swap(
            _snapshot(), _systemd=sd, _exit=exit_fn,
            _source_root="/fake/repo", _runner=runner, _state_dir=str(tmp_path / "state"),
        )

        reset_calls = [c for c in runner.calls if c[0][:3] == ("git", "reset", "--hard")]
        assert reset_calls == [(("git", "reset", "--hard", "NEWSHA"), "/fake/repo")]
        assert exit_fn.calls == [0]

    def test_disarm_and_no_exit_if_swap_raises(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        runner = _runner({
            ("git", "reset", "--hard", "NEWSHA"): FakeResult(returncode=1, stderr="fatal: bad object NEWSHA"),
        })
        sd = FakeSelfReloadDeadman()
        exit_fn = FakeExit()

        with pytest.raises(SelfReloadStateError, match="git reset"):
            arm_and_swap(
                _snapshot(), _systemd=sd, _exit=exit_fn,
                _source_root="/fake/repo", _runner=runner, _state_dir=str(state_dir),
            )

        assert sd.disarmed == [SELF_RELOAD_DEADMAN_UNIT]
        assert exit_fn.calls == []
        assert not (state_dir / "pending.json").exists()

    def test_default_revert_cmd_targets_prod_script(self, tmp_path: Path) -> None:
        sd = FakeSelfReloadDeadman()
        runner = _runner()
        exit_fn = FakeExit()
        snap = _snapshot()

        arm_and_swap(
            snap, _systemd=sd, _exit=exit_fn,
            _source_root="/fake/repo", _runner=runner, _state_dir=str(tmp_path / "state"),
        )

        assert sd.armed[0][2] == f"/usr/local/bin/rawos-selfreload-revert {snap.old_sha} {snap.state_id}"

    def test_injected_revert_cmd_overrides_default(self, tmp_path: Path) -> None:
        """A twin-prove harness must be able to point the deadman at a
        sandboxed revert script instead of the prod
        /usr/local/bin/rawos-selfreload-revert (which hardcodes /root/rawos
        and `systemctl restart rawos`)."""
        sd = FakeSelfReloadDeadman()
        runner = _runner()
        exit_fn = FakeExit()
        snap = _snapshot()
        custom = f"/usr/local/bin/rawos-selfprobe-revert {snap.old_sha} {snap.state_id}"

        arm_and_swap(
            snap, _systemd=sd, _exit=exit_fn,
            _source_root="/fake/repo", _runner=runner, _state_dir=str(tmp_path / "state"),
            _revert_cmd=custom,
        )

        assert sd.armed[0][2] == custom

    def test_single_flight_refuses_second(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        sd = FakeSelfReloadDeadman()
        runner = _runner()
        exit_fn = FakeExit()

        arm_and_swap(
            _snapshot(), _systemd=sd, _exit=exit_fn,
            _source_root="/fake/repo", _runner=runner, _state_dir=str(state_dir),
        )
        assert len(sd.armed) == 1

        with pytest.raises(SelfReloadStateError, match="pending"):
            arm_and_swap(
                _snapshot(new_sha="OTHERSHA"), _systemd=sd, _exit=exit_fn,
                _source_root="/fake/repo", _runner=runner, _state_dir=str(state_dir),
            )

        assert len(sd.armed) == 1  # second arm never happened


# ---------------------------------------------------------------------------
# boot_liveness_commit() — out-of-band verification at boot
# ---------------------------------------------------------------------------

class TestBootLivenessCommit:
    def _write_state(self, state_dir: Path, *, old_sha="OLDSHA", new_sha="NEWSHA", armed_at=0.0) -> Path:
        state_dir.mkdir(parents=True, exist_ok=True)
        state_path = state_dir / "pending.json"
        state_path.write_text(json.dumps({
            "old_sha": old_sha,
            "new_sha": new_sha,
            "state_id": str(uuid.uuid4()),
            "armed_at": armed_at,
            "deadman_unit": SELF_RELOAD_DEADMAN_UNIT,
        }))
        return state_path

    def test_no_pending_noop(self, tmp_path: Path) -> None:
        sd = FakeSelfReloadDeadman()
        runner = _runner()

        result = boot_liveness_commit(
            _systemd=sd, _probe=lambda: True, _now=lambda: 0.0,
            _source_root="/fake/repo", _runner=runner, _state_dir=str(tmp_path / "state"),
        )

        assert result == "no_pending"
        assert sd.disarmed == []

    def test_commits_when_new_sha_healthy(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_path = self._write_state(state_dir, armed_at=900.0)
        runner = _runner({_REV_PARSE_HEAD: FakeResult(stdout="NEWSHA\n")})
        sd = FakeSelfReloadDeadman()

        result = boot_liveness_commit(
            _systemd=sd, _probe=lambda: True, _now=lambda: 1000.0,
            _source_root="/fake/repo", _runner=runner, _state_dir=str(state_dir),
        )

        assert result == "committed"
        assert sd.disarmed == [SELF_RELOAD_DEADMAN_UNIT]
        assert not state_path.exists()

    def test_no_disarm_when_liveness_fails(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_path = self._write_state(state_dir, armed_at=900.0)
        runner = _runner({_REV_PARSE_HEAD: FakeResult(stdout="NEWSHA\n")})
        sd = FakeSelfReloadDeadman()

        # deadline = 900 + DELAY - margin; pick _now past it
        deadline_passed = 900.0 + SELF_RELOAD_DEADMAN_DELAY_S + 1.0
        result = boot_liveness_commit(
            _systemd=sd, _probe=lambda: False, _now=lambda: deadline_passed, _sleep=lambda s: None,
            _source_root="/fake/repo", _runner=runner, _state_dir=str(state_dir),
        )

        assert result == "liveness_failed"
        assert sd.disarmed == []
        assert state_path.exists()  # state retained — deadman timer still armed

    def test_resurrected_disarms_and_clears(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_path = self._write_state(state_dir, armed_at=900.0)
        runner = _runner({_REV_PARSE_HEAD: FakeResult(stdout="OLDSHA\n")})
        sd = FakeSelfReloadDeadman()

        result = boot_liveness_commit(
            _systemd=sd, _probe=lambda: True, _now=lambda: 1000.0,
            _source_root="/fake/repo", _runner=runner, _state_dir=str(state_dir),
        )

        assert result == "resurrected"
        assert sd.disarmed == [SELF_RELOAD_DEADMAN_UNIT]
        assert not state_path.exists()

    def test_probe_retries_until_deadline(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        self._write_state(state_dir, armed_at=0.0)
        runner = _runner({_REV_PARSE_HEAD: FakeResult(stdout="NEWSHA\n")})
        sd = FakeSelfReloadDeadman()

        probe_results = iter([False, False, True])
        result = boot_liveness_commit(
            _systemd=sd, _probe=lambda: next(probe_results),
            _now=lambda: 10.0,  # always well before deadline (0 + DELAY - margin)
            _sleep=lambda s: None,
            _source_root="/fake/repo", _runner=runner, _state_dir=str(state_dir),
        )

        assert result == "committed"
        assert sd.disarmed == [SELF_RELOAD_DEADMAN_UNIT]

    def test_probe_deadline_exceeded_fails(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        self._write_state(state_dir, armed_at=0.0)
        runner = _runner({_REV_PARSE_HEAD: FakeResult(stdout="NEWSHA\n")})
        sd = FakeSelfReloadDeadman()

        deadline_passed = SELF_RELOAD_DEADMAN_DELAY_S + 1.0
        result = boot_liveness_commit(
            _systemd=sd, _probe=lambda: False, _now=lambda: deadline_passed, _sleep=lambda s: None,
            _source_root="/fake/repo", _runner=runner, _state_dir=str(state_dir),
        )

        assert result == "liveness_failed"
        assert sd.disarmed == []


# ---------------------------------------------------------------------------
# Owner-triggered funnel + dormancy (I-SR6)
# ---------------------------------------------------------------------------

class TestOwnerAndDormancy:
    def test_execute_runs_preflight_then_arm(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        runner = _runner()
        worktree = FakeWorktree()
        sd = FakeSelfReloadDeadman()
        exit_fn = FakeExit()

        execute_owner_self_reload(
            "NEWSHA",
            _source_root="/fake/repo", _runner=runner, _worktree=worktree,
            _systemd=sd, _exit=exit_fn, _state_dir=str(state_dir),
        )

        assert worktree.created == [("/fake/repo", "NEWSHA")]  # preflight ran
        assert len(sd.armed) == 1  # arm_and_swap ran
        assert exit_fn.calls == [0]
        record = json.loads((state_dir / "pending.json").read_text())
        assert record["new_sha"] == "NEWSHA"

    def test_autonomous_entrypoint_exists_but_inert_by_default(self) -> None:
        """I-SR6 superseded (Stage 2): operate_on_self_reload now exists, but
        is inert until self_reload_enabled=True AND the operation class is
        graduated (I-SR9) — neither is true on a fresh install."""
        import rawos.kernel.self_reload as self_reload_module
        from rawos.config import settings
        assert hasattr(self_reload_module, "operate_on_self_reload")
        assert settings.self_reload_enabled is False

    def test_flag_defaults_false(self) -> None:
        from rawos.config import settings
        assert settings.self_reload_enabled is False

    def test_autonomous_loop_flag_defaults_false(self) -> None:
        from rawos.config import settings
        assert settings.self_reload_autonomous_enabled is False


# ---------------------------------------------------------------------------
# operate_on_self_reload() — Stage 2 autonomous gate (I-SR9)
# ---------------------------------------------------------------------------

class TestOperateOnSelfReload:
    def setup_method(self) -> None:
        self.tmp = tempfile.mkdtemp()
        db.init(os.path.join(self.tmp, "test.db"))
        self.user = db.create_user(User(
            email=f"self-reload-gate-{id(self)}-{os.getpid()}@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))

    def _graduate(self, target: str) -> None:
        now = int(time.time())
        for i in range(GRADUATION_THRESHOLD * 2):
            db.update_operator_track_record(
                self.user.id, "self_reload", target, verified=True, now=now + i,
            )

    def _runner_with_master(self, master_sha: str, head_sha: str = "OLDSHA") -> FakeRunner:
        return _runner({
            ("git", "rev-parse", "HEAD"): FakeResult(stdout=f"{head_sha}\n"),
            ("git", "rev-parse", "master"): FakeResult(stdout=f"{master_sha}\n"),
        })

    def test_noop_when_master_equals_head(self) -> None:
        runner = self._runner_with_master("OLDSHA", head_sha="OLDSHA")
        outcome = operate_on_self_reload(
            self.user.id, _source_root="/fake/repo", _runner=runner,
        )
        assert outcome == SelfReloadOperateOutcome(
            auto_applied=False, proposed=False, new_sha=None,
            reason="master HEAD == current HEAD; nothing to reload",
        )

    def test_propose_only_when_self_reload_disabled(self, monkeypatch) -> None:
        import rawos.kernel.self_reload as self_reload_module
        monkeypatch.setattr(self_reload_module.settings, "self_reload_enabled", False)
        runner = self._runner_with_master("NEWSHA")

        outcome = operate_on_self_reload(
            self.user.id, _source_root="/fake/repo", _runner=runner,
        )
        assert outcome.auto_applied is False
        assert outcome.proposed is True
        assert outcome.new_sha == "NEWSHA"
        assert "self_reload_enabled=False" in outcome.reason

    def test_propose_only_when_ungraduated(self, monkeypatch) -> None:
        import rawos.kernel.self_reload as self_reload_module
        monkeypatch.setattr(self_reload_module.settings, "self_reload_enabled", True)
        runner = self._runner_with_master("NEWSHA")

        outcome = operate_on_self_reload(
            self.user.id, _source_root="/fake/repo", _runner=runner,
        )
        assert outcome.auto_applied is False
        assert outcome.proposed is True
        assert "graduated" in outcome.reason

    def test_auto_applies_when_enabled_and_graduated(self, tmp_path, monkeypatch) -> None:
        import rawos.kernel.self_reload as self_reload_module
        monkeypatch.setattr(self_reload_module.settings, "self_reload_enabled", True)
        self._graduate("/fake/repo")
        runner = self._runner_with_master("NEWSHA")
        worktree = FakeWorktree()
        sd = FakeSelfReloadDeadman()
        exit_fn = FakeExit()
        state_dir = tmp_path / "state"

        outcome = operate_on_self_reload(
            self.user.id,
            _source_root="/fake/repo", _runner=runner, _worktree=worktree,
            _systemd=sd, _exit=exit_fn, _state_dir=str(state_dir),
        )

        assert outcome.auto_applied is True
        assert outcome.proposed is False
        assert outcome.new_sha == "NEWSHA"
        assert exit_fn.calls == [0]
        record = json.loads((state_dir / "pending.json").read_text())
        assert record["new_sha"] == "NEWSHA"
        assert record["autonomous"] is True

    def test_propose_only_does_not_write_track_record(self, monkeypatch) -> None:
        import rawos.kernel.self_reload as self_reload_module
        monkeypatch.setattr(self_reload_module.settings, "self_reload_enabled", True)
        runner = self._runner_with_master("NEWSHA")

        operate_on_self_reload(self.user.id, _source_root="/fake/repo", _runner=runner)

        track = db.get_operator_track_record(self.user.id, "self_reload", "/fake/repo")
        assert track.verified_successes == 0
        assert track.graduated is False


# ---------------------------------------------------------------------------
# Regression — self-protection floor (operator R2) left untouched (I-SR1)
# ---------------------------------------------------------------------------

class TestRegressionSelfProtectionFloorUnchanged:
    def test_self_protected_services_floor_includes_rawos_and_ssh(self) -> None:
        from rawos.kernel.operator import _SELF_PROTECTED_SERVICES

        for name in ("rawos.service", "rawos", "ssh.service", "ssh", "sshd.service", "sshd"):
            assert name in _SELF_PROTECTED_SERVICES
