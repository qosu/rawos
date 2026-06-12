"""WindowsCrashReporter — Get-WinEvent Level=1 (Critical) from Application log.

Level=1 is Windows Event Log Critical — the closest equivalent to an application
crash recorded by Windows Error Reporting. Level=2 (Error) is used for recent_errors
in WindowsLogReader; Level=1 here is intentionally distinct.

EXPERIMENTAL: never live until a Windows host verifies it.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from rawos.kernel.arch.windows import WindowsCrashReporter


def _mock_run(stdout: str, returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


# ── basic contract ────────────────────────────────────────────────────────────

def test_recent_crashes_returns_list_of_provider_names():
    reporter = WindowsCrashReporter()
    with patch("rawos.kernel.arch.windows.subprocess.run",
               return_value=_mock_run("Chrome\nNotepad\n")):
        result = reporter.recent_crashes("15 minutes ago")
    assert result == ["Chrome", "Notepad"]


def test_recent_crashes_returns_sorted():
    """Protocol contract: sorted unique process names."""
    reporter = WindowsCrashReporter()
    with patch("rawos.kernel.arch.windows.subprocess.run",
               return_value=_mock_run("Notepad\nChrome\n")):
        result = reporter.recent_crashes("15 minutes ago")
    assert result == ["Chrome", "Notepad"]


def test_recent_crashes_deduplicates_process_names():
    """Same provider appearing multiple times must be collapsed to one entry."""
    reporter = WindowsCrashReporter()
    with patch("rawos.kernel.arch.windows.subprocess.run",
               return_value=_mock_run("Chrome\nNotepad\nChrome\n")):
        result = reporter.recent_crashes("15 minutes ago")
    assert result == ["Chrome", "Notepad"]


def test_recent_crashes_returns_empty_on_nonzero_exit():
    reporter = WindowsCrashReporter()
    with patch("rawos.kernel.arch.windows.subprocess.run",
               return_value=_mock_run("", returncode=1)):
        assert reporter.recent_crashes("15 minutes ago") == []


def test_recent_crashes_returns_empty_on_exception():
    reporter = WindowsCrashReporter()
    with patch("rawos.kernel.arch.windows.subprocess.run",
               side_effect=OSError("powershell unavailable")):
        assert reporter.recent_crashes("15 minutes ago") == []


def test_recent_crashes_filters_blank_lines():
    reporter = WindowsCrashReporter()
    with patch("rawos.kernel.arch.windows.subprocess.run",
               return_value=_mock_run("Notepad\n\n   \nChrome\n")):
        result = reporter.recent_crashes("15 minutes ago")
    assert "" not in result
    assert "   " not in result
    assert "Notepad" in result
    assert "Chrome" in result


# ── command shape ─────────────────────────────────────────────────────────────

def test_recent_crashes_command_uses_level_1():
    """Level=1 = Critical. Application crashes appear here, not Level=2 (Error)."""
    reporter = WindowsCrashReporter()
    with patch("rawos.kernel.arch.windows.subprocess.run",
               return_value=_mock_run("")) as mock_run:
        reporter.recent_crashes("15 minutes ago")
    cmd = " ".join(mock_run.call_args[0][0])
    assert "Level=1" in cmd
    assert "Level=2" not in cmd


def test_recent_crashes_command_targets_application_log():
    reporter = WindowsCrashReporter()
    with patch("rawos.kernel.arch.windows.subprocess.run",
               return_value=_mock_run("")) as mock_run:
        reporter.recent_crashes("15 minutes ago")
    cmd = " ".join(mock_run.call_args[0][0])
    assert "Application" in cmd


def test_recent_crashes_command_expands_provider_name():
    reporter = WindowsCrashReporter()
    with patch("rawos.kernel.arch.windows.subprocess.run",
               return_value=_mock_run("")) as mock_run:
        reporter.recent_crashes("15 minutes ago")
    cmd = " ".join(mock_run.call_args[0][0])
    assert "ProviderName" in cmd


def test_recent_crashes_uses_addminutes_for_relative_since():
    reporter = WindowsCrashReporter()
    with patch("rawos.kernel.arch.windows.subprocess.run",
               return_value=_mock_run("")) as mock_run:
        reporter.recent_crashes("15 minutes ago")
    cmd = " ".join(mock_run.call_args[0][0])
    assert "AddMinutes(-15)" in cmd
    assert "AddHours" not in cmd


def test_recent_crashes_uses_addhours_for_hours_since():
    reporter = WindowsCrashReporter()
    with patch("rawos.kernel.arch.windows.subprocess.run",
               return_value=_mock_run("")) as mock_run:
        reporter.recent_crashes("2 hours ago")
    cmd = " ".join(mock_run.call_args[0][0])
    assert "AddHours(-2)" in cmd
    assert "AddMinutes" not in cmd


def test_recent_crashes_passes_iso_timestamp_as_quoted_string():
    reporter = WindowsCrashReporter()
    with patch("rawos.kernel.arch.windows.subprocess.run",
               return_value=_mock_run("")) as mock_run:
        reporter.recent_crashes("2026-01-15 10:30:00")
    cmd = " ".join(mock_run.call_args[0][0])
    assert "2026-01-15 10:30:00" in cmd
    assert "AddMinutes" not in cmd
    assert "AddHours" not in cmd


def test_recent_crashes_uses_powershell_argv_list():
    """Command must be argv list, not shell string — avoids quoting issues."""
    reporter = WindowsCrashReporter()
    with patch("rawos.kernel.arch.windows.subprocess.run",
               return_value=_mock_run("")) as mock_run:
        reporter.recent_crashes("15 minutes ago")
    args = mock_run.call_args[0][0]
    assert isinstance(args, list)
    assert args[0] == "powershell.exe"
