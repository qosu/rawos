"""
kernel/arch/linux — LinuxServiceManager.

Characterization test: list_failed() must reproduce, byte-for-byte, the
`systemctl list-units --type=service --state=failed --no-legend
--no-pager --plain` command and the not-found-skip filter currently
inlined in context/server_scanner.py:_check_failed_services. Stage A is
a zero-behavior-change extraction — this test is the proof.

is_active()/restart()/supports_reversible_apply are new ABI surface
(not yet wired) but built and tested now per base.py's ServiceManager
Protocol.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from rawos.kernel.arch.linux import LinuxServiceManager


def _mock_run(stdout: str, returncode: int = 0) -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    return result


def test_list_failed_runs_systemctl_list_units_failed():
    mgr = LinuxServiceManager()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               return_value=_mock_run("foo.service loaded failed failed Foo\n")) as mock_run:
        result = mgr.list_failed()

    mock_run.assert_called_once_with(
        ["systemctl", "list-units", "--type=service", "--state=failed",
         "--no-legend", "--no-pager", "--plain"],
        capture_output=True, text=True, timeout=5.0,
    )
    assert result == ["foo.service"]


def test_list_failed_skips_not_found_units():
    mgr = LinuxServiceManager()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               return_value=_mock_run("stale.service not-found failed failed Stale\n")):
        assert mgr.list_failed() == []


def test_list_failed_returns_empty_on_nonzero_exit():
    mgr = LinuxServiceManager()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               return_value=_mock_run("", returncode=1)):
        assert mgr.list_failed() == []


def test_list_failed_returns_empty_on_blank_output():
    mgr = LinuxServiceManager()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               return_value=_mock_run("   \n")):
        assert mgr.list_failed() == []


def test_list_failed_returns_empty_on_exception():
    mgr = LinuxServiceManager()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               side_effect=OSError("boom")):
        assert mgr.list_failed() == []


def test_is_active_true_when_systemctl_reports_active():
    mgr = LinuxServiceManager()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               return_value=_mock_run("active\n")) as mock_run:
        assert mgr.is_active("rawos.service") is True

    mock_run.assert_called_once_with(
        ["systemctl", "is-active", "rawos.service"],
        capture_output=True, text=True, timeout=3.0,
    )


def test_is_active_false_when_systemctl_reports_inactive():
    mgr = LinuxServiceManager()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               return_value=_mock_run("inactive\n")):
        assert mgr.is_active("rawos.service") is False


def test_is_active_false_on_exception():
    mgr = LinuxServiceManager()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               side_effect=OSError("boom")):
        assert mgr.is_active("rawos.service") is False


def test_restart_runs_systemctl_restart():
    mgr = LinuxServiceManager()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               return_value=_mock_run("")) as mock_run:
        mgr.restart("rawos.service")

    mock_run.assert_called_once_with(
        ["systemctl", "restart", "rawos.service"],
        capture_output=True, text=True, timeout=30.0,
    )


def test_restart_returns_true_on_success():
    mgr = LinuxServiceManager()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               return_value=_mock_run("", returncode=0)):
        assert mgr.restart("rawos.service") is True


def test_restart_returns_false_on_failure():
    mgr = LinuxServiceManager()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               return_value=_mock_run("", returncode=1)):
        assert mgr.restart("rawos.service") is False


def test_restart_returns_false_on_exception():
    mgr = LinuxServiceManager()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               side_effect=OSError("systemd unavailable")):
        assert mgr.restart("rawos.service") is False


def test_supports_reversible_apply_is_true_on_linux():
    assert LinuxServiceManager().supports_reversible_apply is True
