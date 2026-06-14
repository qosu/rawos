"""tests/test_pam_operator.py — TDD for pam_operator module (Phase 22).

TDD Iron Law: this file must go RED before pam_operator.py is written.

Injectable overrides used throughout:
  _pam_dir    — tmp_path / "pam.d"
  _backup_dir — tmp_path / "backups"
  _probe_fn   — lambda: True|False  (avoids real SSH)
  _systemd    — FakePamDeadman (avoids real systemd-run)
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from rawos.kernel.pam_operator import (
    PamFileEdit,
    PamInstallError,
    PamRefusalError,
    commit_pam_edit,
    install_pam_edit_with_deadman,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    pam_dir = tmp_path / "pam.d"
    pam_dir.mkdir()
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    return pam_dir, backup_dir


def _op(
    tmp_path: Path,
    pam_file: str = "rawos-guest",
    content: bytes = b"pam content",
    probe_fn=None,
) -> tuple[PamFileEdit, Path, Path]:
    pam_dir, backup_dir = _dirs(tmp_path)
    op = PamFileEdit(
        pam_file, content,
        _probe_fn=probe_fn,
        _pam_dir=pam_dir,
        _backup_dir=backup_dir,
    )
    return op, pam_dir, backup_dir


class FakePamDeadman:
    def __init__(self) -> None:
        self.armed: list[tuple[str, int, str]] = []
        self.disarmed: list[str] = []

    def arm(self, unit: str, delay_s: int, revert_cmd: str) -> None:
        self.armed.append((unit, delay_s, revert_cmd))

    def disarm(self, unit: str) -> None:
        self.disarmed.append(unit)


# ---------------------------------------------------------------------------
# Self-protection refusal (refuse-at-construction, cannot be suppressed)
# ---------------------------------------------------------------------------

class TestSelfProtection:
    @pytest.mark.parametrize("name", [
        "sshd", "common-auth", "common-account", "common-password",
        "common-session", "common-session-noninteractive",
        "su", "sudo", "sudo-i", "login", "runuser", "runuser-l",
        "chfn", "chpasswd", "chsh", "passwd", "newusers", "other",
        "atd", "cron", "vmtoolsd",
    ])
    def test_refuses_all_protected_files(self, name: str, tmp_path: Path) -> None:
        pam_dir, backup_dir = _dirs(tmp_path)
        with pytest.raises(PamRefusalError):
            PamFileEdit(name, b"content", _pam_dir=pam_dir, _backup_dir=backup_dir)

    def test_allows_non_protected_file(self, tmp_path: Path) -> None:
        pam_dir, backup_dir = _dirs(tmp_path)
        op = PamFileEdit("rawos-guest", b"content", _pam_dir=pam_dir, _backup_dir=backup_dir)
        assert op is not None

    def test_protected_file_in_install_never_arms_deadman(self, tmp_path: Path) -> None:
        fake_sd = FakePamDeadman()
        pam_dir, backup_dir = _dirs(tmp_path)
        with pytest.raises(PamRefusalError):
            install_pam_edit_with_deadman(
                "sshd", b"content",
                _systemd=fake_sd,
                _probe_fn=lambda: True,
                _pam_dir=pam_dir,
                _backup_dir=backup_dir,
            )
        assert fake_sd.armed == []


# ---------------------------------------------------------------------------
# capture()
# ---------------------------------------------------------------------------

class TestCapture:
    def test_existing_file_records_content(self, tmp_path: Path) -> None:
        op, pam_dir, backup_dir = _op(tmp_path)
        (pam_dir / "rawos-guest").write_bytes(b"original pam config")
        snap = op.capture()
        assert snap.pam_file == "rawos-guest"
        assert snap.was_absent is False
        assert snap.prior_content == b"original pam config"

    def test_existing_file_writes_backup_to_disk(self, tmp_path: Path) -> None:
        op, pam_dir, backup_dir = _op(tmp_path)
        (pam_dir / "rawos-guest").write_bytes(b"original")
        snap = op.capture()
        assert Path(snap.backup_path).read_bytes() == b"original"

    def test_absent_file_marks_was_absent(self, tmp_path: Path) -> None:
        op, pam_dir, backup_dir = _op(tmp_path, "rawos-newservice")
        snap = op.capture()
        assert snap.was_absent is True
        assert snap.prior_content == b""

    def test_absent_file_writes_empty_backup(self, tmp_path: Path) -> None:
        op, pam_dir, backup_dir = _op(tmp_path, "rawos-newservice")
        snap = op.capture()
        assert Path(snap.backup_path).read_bytes() == b""

    def test_snapshot_id_is_uuid_format(self, tmp_path: Path) -> None:
        import re
        op, _, _ = _op(tmp_path)
        snap = op.capture()
        assert re.match(
            r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
            snap.snapshot_id,
        )

    def test_backup_path_in_backup_dir(self, tmp_path: Path) -> None:
        op, pam_dir, backup_dir = _op(tmp_path)
        snap = op.capture()
        assert Path(snap.backup_path).parent == backup_dir


# ---------------------------------------------------------------------------
# apply()
# ---------------------------------------------------------------------------

class TestApply:
    def test_writes_new_content(self, tmp_path: Path) -> None:
        op, pam_dir, _ = _op(tmp_path, content=b"new pam config")
        op.apply()
        assert (pam_dir / "rawos-guest").read_bytes() == b"new pam config"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        op, pam_dir, _ = _op(tmp_path, content=b"new")
        (pam_dir / "rawos-guest").write_bytes(b"old")
        op.apply()
        assert (pam_dir / "rawos-guest").read_bytes() == b"new"


# ---------------------------------------------------------------------------
# verify()
# ---------------------------------------------------------------------------

class TestVerify:
    def test_probe_returns_true(self, tmp_path: Path) -> None:
        op, _, _ = _op(tmp_path, probe_fn=lambda: True)
        assert op.verify() is True

    def test_probe_returns_false(self, tmp_path: Path) -> None:
        op, _, _ = _op(tmp_path, probe_fn=lambda: False)
        assert op.verify() is False


# ---------------------------------------------------------------------------
# restore()
# ---------------------------------------------------------------------------

class TestRestore:
    def test_restores_existing_file_from_disk(self, tmp_path: Path) -> None:
        op, pam_dir, _ = _op(tmp_path, content=b"new")
        (pam_dir / "rawos-guest").write_bytes(b"original")
        snap = op.capture()
        op.apply()
        assert (pam_dir / "rawos-guest").read_bytes() == b"new"
        op.restore(snap)
        assert (pam_dir / "rawos-guest").read_bytes() == b"original"

    def test_restore_absent_removes_file(self, tmp_path: Path) -> None:
        op, pam_dir, _ = _op(tmp_path, pam_file="rawos-newservice", content=b"content")
        snap = op.capture()
        assert snap.was_absent is True
        op.apply()
        assert (pam_dir / "rawos-newservice").exists()
        op.restore(snap)
        assert not (pam_dir / "rawos-newservice").exists()

    def test_restore_reads_backup_not_memory(self, tmp_path: Path) -> None:
        """restore reads backup_path on disk — correct even if in-memory snap is wrong."""
        op, pam_dir, backup_dir = _op(tmp_path, content=b"new")
        (pam_dir / "rawos-guest").write_bytes(b"original")
        snap = op.capture()
        # tamper backup on disk — restore must use disk, not snap.prior_content
        Path(snap.backup_path).write_bytes(b"disk-version")
        op.apply()
        op.restore(snap)
        assert (pam_dir / "rawos-guest").read_bytes() == b"disk-version"


# ---------------------------------------------------------------------------
# install_pam_edit_with_deadman()
# ---------------------------------------------------------------------------

class TestInstallWithDeadman:
    def test_arms_before_apply(self, tmp_path: Path) -> None:
        pam_dir, backup_dir = _dirs(tmp_path)
        apply_order: list[str] = []
        fake_sd = FakePamDeadman()

        class _TrackingEdit(PamFileEdit):
            def apply(self):
                apply_order.append("apply")
                super().apply()

        # We can't inject a tracking edit easily, so verify order via probe side-effect
        probe_calls: list[int] = []

        def _probe():
            probe_calls.append(len(fake_sd.armed))
            return True

        snap_id = install_pam_edit_with_deadman(
            "rawos-guest", b"content",
            _systemd=fake_sd,
            _probe_fn=_probe,
            _pam_dir=pam_dir,
            _backup_dir=backup_dir,
        )
        assert len(fake_sd.armed) == 1
        # probe ran after arm
        assert probe_calls[0] == 1

    def test_verify_fail_disarms_and_restores(self, tmp_path: Path) -> None:
        pam_dir, backup_dir = _dirs(tmp_path)
        (pam_dir / "rawos-guest").write_bytes(b"original")
        fake_sd = FakePamDeadman()

        with pytest.raises(PamInstallError):
            install_pam_edit_with_deadman(
                "rawos-guest", b"new",
                _systemd=fake_sd,
                _probe_fn=lambda: False,
                _pam_dir=pam_dir,
                _backup_dir=backup_dir,
            )

        assert fake_sd.disarmed == ["rawos-pam-revert"]
        assert (pam_dir / "rawos-guest").read_bytes() == b"original"

    def test_verify_pass_returns_snapshot_id_and_stays_armed(self, tmp_path: Path) -> None:
        pam_dir, backup_dir = _dirs(tmp_path)
        fake_sd = FakePamDeadman()

        snap_id = install_pam_edit_with_deadman(
            "rawos-guest", b"new",
            _systemd=fake_sd,
            _probe_fn=lambda: True,
            _pam_dir=pam_dir,
            _backup_dir=backup_dir,
        )

        assert isinstance(snap_id, str) and len(snap_id) == 36  # UUID
        assert fake_sd.disarmed == []  # still armed
        assert (pam_dir / "rawos-guest").read_bytes() == b"new"

    def test_revert_cmd_contains_snapshot_id_and_pam_file(self, tmp_path: Path) -> None:
        pam_dir, backup_dir = _dirs(tmp_path)
        fake_sd = FakePamDeadman()

        snap_id = install_pam_edit_with_deadman(
            "rawos-guest", b"content",
            _systemd=fake_sd,
            _probe_fn=lambda: True,
            _pam_dir=pam_dir,
            _backup_dir=backup_dir,
        )

        _unit, _delay, revert_cmd = fake_sd.armed[0]
        assert snap_id in revert_cmd
        assert "rawos-guest" in revert_cmd

    def test_unexpected_exception_disarms_and_restores(self, tmp_path: Path) -> None:
        pam_dir, backup_dir = _dirs(tmp_path)
        (pam_dir / "rawos-guest").write_bytes(b"original")
        fake_sd = FakePamDeadman()

        def _exploding_probe():
            raise RuntimeError("unexpected failure")

        with pytest.raises(PamInstallError) as exc_info:
            install_pam_edit_with_deadman(
                "rawos-guest", b"new",
                _systemd=fake_sd,
                _probe_fn=_exploding_probe,
                _pam_dir=pam_dir,
                _backup_dir=backup_dir,
            )

        assert fake_sd.disarmed == ["rawos-pam-revert"]
        assert (pam_dir / "rawos-guest").read_bytes() == b"original"
        assert "unexpected failure" in str(exc_info.value)


# ---------------------------------------------------------------------------
# commit_pam_edit()
# ---------------------------------------------------------------------------

class TestCommitPamEdit:
    def test_disarms_deadman(self) -> None:
        fake_sd = FakePamDeadman()
        commit_pam_edit(_systemd=fake_sd)
        assert fake_sd.disarmed == ["rawos-pam-revert"]
