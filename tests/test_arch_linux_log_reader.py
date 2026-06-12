"""
kernel/arch/linux — LinuxLogReader.

Characterization test: tail()/recent_errors() must reproduce,
byte-for-byte, the `journalctl ...` commands currently inlined in
context/server_scanner.py:_check_failed_services (tail) and
_check_recent_errors (recent_errors). Stage A is a zero-behavior-change
extraction — this test is the proof.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from rawos.kernel.arch.linux import LinuxLogReader


def _mock_run(stdout: str, returncode: int = 0) -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    return result


def test_tail_runs_journalctl_short_monotonic():
    reader = LinuxLogReader()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               return_value=_mock_run("line1\nline2\n")) as mock_run:
        out = reader.tail("foo.service", 8)

    mock_run.assert_called_once_with(
        ["journalctl", "-u", "foo.service", "-n", "8", "--no-pager", "-q",
         "--output=short-monotonic"],
        capture_output=True, text=True, timeout=3.0,
    )
    assert out == "line1\nline2"


def test_tail_returns_empty_string_on_nonzero_exit():
    reader = LinuxLogReader()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               return_value=_mock_run("", returncode=1)):
        assert reader.tail("foo.service", 8) == ""


def test_tail_returns_empty_string_on_exception():
    reader = LinuxLogReader()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               side_effect=OSError("boom")):
        assert reader.tail("foo.service", 8) == ""


def test_recent_errors_runs_journalctl_since_priority_err():
    reader = LinuxLogReader()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               return_value=_mock_run("err line\n")) as mock_run:
        out = reader.recent_errors("foo.service", "15 minutes ago")

    mock_run.assert_called_once_with(
        ["journalctl", "-u", "foo.service", "--since", "15 minutes ago",
         "-p", "err", "-q", "--no-pager", "--output=short"],
        capture_output=True, text=True, timeout=3.0,
    )
    assert out == "err line"


def test_recent_errors_returns_empty_string_on_nonzero_exit():
    reader = LinuxLogReader()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               return_value=_mock_run("", returncode=1)):
        assert reader.recent_errors("foo.service", "15 minutes ago") == ""


def test_recent_errors_returns_empty_string_on_exception():
    reader = LinuxLogReader()
    with patch("rawos.kernel.arch.linux.subprocess.run",
               side_effect=OSError("boom")):
        assert reader.recent_errors("foo.service", "15 minutes ago") == ""
