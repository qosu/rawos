"""tests/test_arch_file_operator.py — TDD for the FileOperator arch ABI Protocol.

Real filesystem ops on tmp_path (no mocking) for read/write/backup/restore.
Self-protection refusals are checked against the real protected-path constants
without ever touching those paths.
"""
from __future__ import annotations

import pytest

from rawos.kernel.arch.base import FileOperatorRefusalError, FileSnapshot
from rawos.kernel.arch.linux import LinuxFileOperator
from rawos.kernel.arch.macos import MacOSFileOperator
from rawos.kernel.arch.windows import WindowsFileOperator


# ---------------------------------------------------------------------------
# Linux: read/write/exists/backup/restore on real tmp paths
# ---------------------------------------------------------------------------

def test_write_then_read_roundtrip(tmp_path):
    op = LinuxFileOperator()
    target = str(tmp_path / "config.conf")

    op.write(target, b"hello rawos\n")

    assert op.read(target) == b"hello rawos\n"


def test_read_returns_none_for_missing_file(tmp_path):
    op = LinuxFileOperator()
    target = str(tmp_path / "missing.conf")

    assert op.read(target) is None


def test_exists_true_for_existing_file(tmp_path):
    op = LinuxFileOperator()
    target = str(tmp_path / "present.conf")
    op.write(target, b"x")

    assert op.exists(target) is True


def test_exists_false_for_missing_file(tmp_path):
    op = LinuxFileOperator()
    target = str(tmp_path / "missing.conf")

    assert op.exists(target) is False


def test_backup_restore_roundtrip_restores_original_bytes(tmp_path):
    op = LinuxFileOperator()
    target = str(tmp_path / "config.conf")
    op.write(target, b"original content\n")

    snapshot = op.backup(target)
    op.write(target, b"mutated content\n")
    op.restore(snapshot)

    assert op.read(target) == b"original content\n"


def test_restore_of_absent_deletes_file(tmp_path):
    op = LinuxFileOperator()
    target = str(tmp_path / "new.conf")

    snapshot = op.backup(target)  # target does not exist yet
    op.write(target, b"newly written\n")
    op.restore(snapshot)

    assert op.exists(target) is False


def test_backup_of_absent_marks_snapshot_not_existed(tmp_path):
    op = LinuxFileOperator()
    target = str(tmp_path / "never-created.conf")

    snapshot = op.backup(target)

    assert isinstance(snapshot, FileSnapshot)
    assert snapshot.existed is False
    assert snapshot.content is None


# ---------------------------------------------------------------------------
# Self-protection refusals (never touch the real protected paths)
# ---------------------------------------------------------------------------

def test_write_refuses_rawos_service_unit():
    op = LinuxFileOperator()

    with pytest.raises(FileOperatorRefusalError):
        op.write("/etc/systemd/system/rawos.service", b"malicious\n")


def test_write_refuses_sshd_frontdoor_config():
    op = LinuxFileOperator()

    with pytest.raises(FileOperatorRefusalError):
        op.write("/etc/ssh/sshd_config.d/50-rawos-frontdoor.conf", b"malicious\n")


def test_write_refuses_rawos_source_tree():
    op = LinuxFileOperator()

    with pytest.raises(FileOperatorRefusalError):
        op.write("/root/rawos/rawos/config.py", b"malicious\n")


def test_write_refuses_rawos_db_under_source_tree():
    op = LinuxFileOperator()

    with pytest.raises(FileOperatorRefusalError):
        op.write("/root/rawos/data/rawos.db", b"malicious\n")


def test_restore_refuses_protected_target():
    op = LinuxFileOperator()
    snapshot = FileSnapshot(path="/etc/systemd/system/rawos.service", existed=True, content=b"x")

    with pytest.raises(FileOperatorRefusalError):
        op.restore(snapshot)


# ---------------------------------------------------------------------------
# supports_file_ops flag per backend
# ---------------------------------------------------------------------------

def test_supports_file_ops_true_on_linux():
    assert LinuxFileOperator().supports_file_ops is True


def test_supports_file_ops_false_on_macos():
    assert MacOSFileOperator().supports_file_ops is False


def test_supports_file_ops_false_on_windows():
    assert WindowsFileOperator().supports_file_ops is False


# ---------------------------------------------------------------------------
# Backend wiring
# ---------------------------------------------------------------------------

def test_get_arch_linux_wires_file_operator(monkeypatch):
    from rawos.kernel.arch import get_arch

    monkeypatch.setattr("sys.platform", "linux")
    backend = get_arch()

    assert isinstance(backend.file_operator, LinuxFileOperator)
