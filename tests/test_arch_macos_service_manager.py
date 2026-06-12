"""
MacOSServiceManager — launchctl-backed service management.

Key design decisions (all documented, not hidden):
- supports_reversible_apply=False: structural gate; auto-apply cannot fire on macOS
  until a live canary test verifies launchctl kickstart atomic behavior.
- list_failed(): launchd marks "failed" as: PID="-" (not running) AND Status!=0.
  Services currently running with a non-zero *previous* exit code are NOT counted.
- is_active(): launchctl list <label> returns 0 + "PID" key in output when running.
- restart(): kickstart -k in the system domain. Only for manual use; auto-apply gate
  blocks this path via supports_reversible_apply=False.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from rawos.kernel.arch.macos import MacOSServiceManager


def _mock_run(stdout: str, returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


# ── supports_reversible_apply ────────────────────────────────────────────────

def test_supports_reversible_apply_is_false():
    mgr = MacOSServiceManager()
    assert mgr.supports_reversible_apply is False


# ── list_failed ──────────────────────────────────────────────────────────────

_LAUNCHCTL_LIST_OUTPUT = (
    "PID\tStatus\tLabel\n"
    "-\t0\tcom.apple.ok\n"
    "-\t1\tcom.example.crashed\n"
    "123\t0\tcom.apple.running\n"
    "-\t78\tcom.example.also_failed\n"
)


def test_list_failed_returns_labels_with_nonzero_status_and_no_pid():
    mgr = MacOSServiceManager()
    with patch("rawos.kernel.arch.macos.subprocess.run",
               return_value=_mock_run(_LAUNCHCTL_LIST_OUTPUT)):
        result = mgr.list_failed()
    assert result == ["com.example.crashed", "com.example.also_failed"]


def test_list_failed_excludes_running_service_with_nonzero_prior_exit():
    """A service with a PID is running — not failed, regardless of Status."""
    mgr = MacOSServiceManager()
    output = "PID\tStatus\tLabel\n456\t1\tcom.example.running_but_bad_prev\n"
    with patch("rawos.kernel.arch.macos.subprocess.run",
               return_value=_mock_run(output)):
        assert mgr.list_failed() == []


def test_list_failed_returns_empty_on_subprocess_failure():
    mgr = MacOSServiceManager()
    with patch("rawos.kernel.arch.macos.subprocess.run",
               return_value=_mock_run("", returncode=1)):
        assert mgr.list_failed() == []


def test_list_failed_returns_empty_on_exception():
    mgr = MacOSServiceManager()
    with patch("rawos.kernel.arch.macos.subprocess.run",
               side_effect=OSError("launchctl unavailable")):
        assert mgr.list_failed() == []


# ── is_active ────────────────────────────────────────────────────────────────

_RUNNING_OUTPUT = '{\n\t"Label" = "com.example.svc";\n\t"PID" = 12345;\n}\n'
_STOPPED_OUTPUT = '{\n\t"Label" = "com.example.svc";\n\t"LastExitStatus" = 0;\n}\n'


def test_is_active_returns_true_when_pid_present():
    mgr = MacOSServiceManager()
    with patch("rawos.kernel.arch.macos.subprocess.run",
               return_value=_mock_run(_RUNNING_OUTPUT)):
        assert mgr.is_active("com.example.svc") is True


def test_is_active_returns_false_when_pid_absent():
    mgr = MacOSServiceManager()
    with patch("rawos.kernel.arch.macos.subprocess.run",
               return_value=_mock_run(_STOPPED_OUTPUT)):
        assert mgr.is_active("com.example.svc") is False


def test_is_active_returns_false_on_nonzero_exit():
    """launchctl list <label> exits 113 when the label is not loaded."""
    mgr = MacOSServiceManager()
    with patch("rawos.kernel.arch.macos.subprocess.run",
               return_value=_mock_run("", returncode=113)):
        assert mgr.is_active("com.example.missing") is False


def test_is_active_returns_false_on_exception():
    mgr = MacOSServiceManager()
    with patch("rawos.kernel.arch.macos.subprocess.run",
               side_effect=OSError("launchctl unavailable")):
        assert mgr.is_active("com.example.svc") is False


# ── restart ──────────────────────────────────────────────────────────────────

def test_restart_calls_kickstart_k_in_system_domain():
    mgr = MacOSServiceManager()
    with patch("rawos.kernel.arch.macos.subprocess.run",
               return_value=_mock_run("", returncode=0)) as mock_run:
        result = mgr.restart("com.example.svc")
    assert result is True
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args == ["launchctl", "kickstart", "-k", "system/com.example.svc"]


def test_restart_returns_false_on_nonzero_exit():
    mgr = MacOSServiceManager()
    with patch("rawos.kernel.arch.macos.subprocess.run",
               return_value=_mock_run("", returncode=1)):
        assert mgr.restart("com.example.svc") is False


def test_restart_returns_false_on_exception():
    mgr = MacOSServiceManager()
    with patch("rawos.kernel.arch.macos.subprocess.run",
               side_effect=OSError("launchctl unavailable")):
        assert mgr.restart("com.example.svc") is False
