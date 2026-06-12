"""
LinuxFrontDoor backend tests.

TDD: these tests are written FIRST. They fail until the production code
is in place.

No real sshd mutations: subprocess is mocked throughout.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ENTRY_CMD = "/usr/local/bin/rawos frontdoor enter"
_EXPECTED_DROPIN_FRAGMENT = (
    "Match User root\n"
    f"    ForceCommand {_ENTRY_CMD}\n"
)


def _mock_run(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


# ---------------------------------------------------------------------------
# Drop-in content contract
# ---------------------------------------------------------------------------

class TestLinuxFrontDoorDropin:
    def test_install_writes_correct_dropin_block(self):
        """install() must write the exact Match User / ForceCommand block."""
        from rawos.kernel.arch.linux import LinuxFrontDoor
        with tempfile.TemporaryDirectory() as tmpdir:
            dropin = Path(tmpdir) / "50-rawos-frontdoor.conf"
            fd = LinuxFrontDoor(dropin_path=dropin)
            fd.install(_ENTRY_CMD)
            content = dropin.read_text()
        assert "Match User root" in content
        assert f"ForceCommand {_ENTRY_CMD}" in content

    def test_install_dropin_contains_managed_comment(self):
        from rawos.kernel.arch.linux import LinuxFrontDoor
        with tempfile.TemporaryDirectory() as tmpdir:
            dropin = Path(tmpdir) / "50-rawos-frontdoor.conf"
            fd = LinuxFrontDoor(dropin_path=dropin)
            fd.install(_ENTRY_CMD)
            content = dropin.read_text()
        assert "Managed by rawos" in content


# ---------------------------------------------------------------------------
# validate() shells sshd -t
# ---------------------------------------------------------------------------

class TestLinuxFrontDoorValidate:
    def test_validate_runs_sshd_dash_t(self):
        from rawos.kernel.arch.linux import LinuxFrontDoor
        with tempfile.TemporaryDirectory() as tmpdir:
            dropin = Path(tmpdir) / "50-rawos-frontdoor.conf"
            fd = LinuxFrontDoor(dropin_path=dropin)
            with patch("rawos.kernel.arch.linux.subprocess.run",
                       return_value=_mock_run(returncode=0)) as mock_run:
                result = fd.validate()
        called_cmd = mock_run.call_args[0][0]
        assert "sshd" in called_cmd[0], f"Expected sshd binary, got {called_cmd[0]}"
        assert called_cmd[1] == "-t"
        assert mock_run.call_args[1] == dict(capture_output=True, text=True, timeout=5.0)
        assert result is True

    def test_validate_returns_false_on_nonzero_exit(self):
        from rawos.kernel.arch.linux import LinuxFrontDoor
        with tempfile.TemporaryDirectory() as tmpdir:
            dropin = Path(tmpdir) / "50-rawos-frontdoor.conf"
            fd = LinuxFrontDoor(dropin_path=dropin)
            with patch("rawos.kernel.arch.linux.subprocess.run",
                       return_value=_mock_run(returncode=1)):
                result = fd.validate()
        assert result is False

    def test_validate_returns_false_on_exception(self):
        from rawos.kernel.arch.linux import LinuxFrontDoor
        with tempfile.TemporaryDirectory() as tmpdir:
            dropin = Path(tmpdir) / "50-rawos-frontdoor.conf"
            fd = LinuxFrontDoor(dropin_path=dropin)
            with patch("rawos.kernel.arch.linux.subprocess.run",
                       side_effect=Exception("no sshd")):
                result = fd.validate()
        assert result is False


# ---------------------------------------------------------------------------
# reload() shells systemctl reload ssh
# ---------------------------------------------------------------------------

class TestLinuxFrontDoorReload:
    def test_reload_calls_systemctl_reload_ssh(self):
        from rawos.kernel.arch.linux import LinuxFrontDoor
        with tempfile.TemporaryDirectory() as tmpdir:
            dropin = Path(tmpdir) / "50-rawos-frontdoor.conf"
            fd = LinuxFrontDoor(dropin_path=dropin)
            with patch("rawos.kernel.arch.linux.subprocess.run",
                       return_value=_mock_run()) as mock_run:
                fd.reload()
        mock_run.assert_called_once_with(
            ["systemctl", "reload", "ssh"],
            capture_output=True, text=True, timeout=10.0,
        )


# ---------------------------------------------------------------------------
# snapshot() / restore() / uninstall() / state()
# ---------------------------------------------------------------------------

class TestLinuxFrontDoorSnapshotRestore:
    def test_snapshot_returns_string_path(self):
        from rawos.kernel.arch.linux import LinuxFrontDoor
        with tempfile.TemporaryDirectory() as tmpdir:
            dropin = Path(tmpdir) / "50-rawos-frontdoor.conf"
            dropin.write_text("existing content")
            fd = LinuxFrontDoor(dropin_path=dropin)
            snap = fd.snapshot()
        assert isinstance(snap, str)
        assert len(snap) > 0

    def test_restore_reinstates_previous_content(self):
        from rawos.kernel.arch.linux import LinuxFrontDoor
        with tempfile.TemporaryDirectory() as tmpdir:
            dropin = Path(tmpdir) / "50-rawos-frontdoor.conf"
            original = "# old content\n"
            dropin.write_text(original)
            fd = LinuxFrontDoor(dropin_path=dropin)
            snap = fd.snapshot()
            # overwrite dropin to simulate a change
            dropin.write_text("# new content\n")
            fd.restore(snap)
            assert dropin.read_text() == original

    def test_snapshot_of_missing_dropin_returns_sentinel(self):
        """If no dropin exists yet, snapshot() must return a valid restore token."""
        from rawos.kernel.arch.linux import LinuxFrontDoor
        with tempfile.TemporaryDirectory() as tmpdir:
            dropin = Path(tmpdir) / "50-rawos-frontdoor.conf"
            fd = LinuxFrontDoor(dropin_path=dropin)
            snap = fd.snapshot()
        assert isinstance(snap, str)
        assert len(snap) > 0

    def test_restore_of_missing_dropin_removes_it(self):
        """Restoring to a 'no dropin' state must delete the dropin if it exists."""
        from rawos.kernel.arch.linux import LinuxFrontDoor
        with tempfile.TemporaryDirectory() as tmpdir:
            dropin = Path(tmpdir) / "50-rawos-frontdoor.conf"
            fd = LinuxFrontDoor(dropin_path=dropin)
            # snapshot when dropin doesn't exist
            snap = fd.snapshot()
            # create a dropin
            dropin.write_text("# added\n")
            # restore to 'no dropin'
            fd.restore(snap)
            assert not dropin.exists()

    def test_uninstall_removes_dropin(self):
        from rawos.kernel.arch.linux import LinuxFrontDoor
        with tempfile.TemporaryDirectory() as tmpdir:
            dropin = Path(tmpdir) / "50-rawos-frontdoor.conf"
            dropin.write_text(_EXPECTED_DROPIN_FRAGMENT)
            fd = LinuxFrontDoor(dropin_path=dropin)
            fd.uninstall()
        assert not dropin.exists()

    def test_uninstall_is_idempotent_when_dropin_missing(self):
        from rawos.kernel.arch.linux import LinuxFrontDoor
        with tempfile.TemporaryDirectory() as tmpdir:
            dropin = Path(tmpdir) / "50-rawos-frontdoor.conf"
            fd = LinuxFrontDoor(dropin_path=dropin)
            fd.uninstall()  # must not raise
        assert not dropin.exists()

    def test_state_installed_when_dropin_exists(self):
        from rawos.kernel.arch.linux import LinuxFrontDoor
        with tempfile.TemporaryDirectory() as tmpdir:
            dropin = Path(tmpdir) / "50-rawos-frontdoor.conf"
            dropin.write_text(
                "# Managed by rawos — do not edit manually.\n"
                "Match User root\n"
                f"    ForceCommand {_ENTRY_CMD}\n"
            )
            fd = LinuxFrontDoor(dropin_path=dropin)
            s = fd.state()
        assert s.installed is True
        assert s.entry_command == _ENTRY_CMD

    def test_state_not_installed_when_dropin_missing(self):
        from rawos.kernel.arch.linux import LinuxFrontDoor
        with tempfile.TemporaryDirectory() as tmpdir:
            dropin = Path(tmpdir) / "50-rawos-frontdoor.conf"
            fd = LinuxFrontDoor(dropin_path=dropin)
            s = fd.state()
        assert s.installed is False
        assert s.entry_command is None

# ---------------------------------------------------------------------------
# enter command — sftp subsystem passthrough
# ---------------------------------------------------------------------------

class TestFrontDoorEnterSftpPassthrough:
    """enter must exec sftp-server directly for SSH_ORIGINAL_COMMAND='subsystem sftp'."""

    def _run_enter(self, ssh_cmd: str, mock_execv, mock_execvp):
        import os
        from unittest.mock import patch, MagicMock
        from click.testing import CliRunner
        from rawos.cli.main import cli

        runner = CliRunner()
        with patch.dict(os.environ, {"SSH_ORIGINAL_COMMAND": ssh_cmd, "SHELL": "/bin/bash"}), \
             patch("rawos.cli.main._load_creds", return_value={"access_token": "tok"}), \
             patch("rawos.cli.main._DEFAULT_URL", "http://127.0.0.1:8002"), \
             patch("httpx.get", return_value=MagicMock(status_code=200)), \
             patch("os.execv", mock_execv), \
             patch("os.execvp", mock_execvp):
            result = runner.invoke(cli, ["frontdoor", "enter"])
        return result

    def test_sftp_subsystem_execs_sftp_server_binary(self):
        from unittest.mock import MagicMock
        mock_execv = MagicMock(side_effect=SystemExit(0))
        mock_execvp = MagicMock(side_effect=SystemExit(0))
        self._run_enter("subsystem sftp", mock_execv, mock_execvp)
        mock_execv.assert_called_once()
        called_path = mock_execv.call_args[0][0]
        assert "sftp-server" in called_path, f"Expected sftp-server binary, got {called_path}"
        mock_execvp.assert_not_called()

    def test_regular_command_uses_shell_passthrough(self):
        """Non-sftp commands still go through shell -c."""
        from unittest.mock import MagicMock
        mock_execv = MagicMock(side_effect=SystemExit(0))
        mock_execvp = MagicMock(side_effect=SystemExit(0))
        self._run_enter("git-upload-pack '/repo'", mock_execv, mock_execvp)
        mock_execvp.assert_called_once()
        called = mock_execvp.call_args[0]
        assert called[0] == "/bin/bash"
        assert called[1] == ["/bin/bash", "-c", "git-upload-pack '/repo'"]
        mock_execv.assert_not_called()
