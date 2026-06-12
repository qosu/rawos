"""
MacOSLogReader — macOS Unified Log (log show) backend.

Key design decisions:
- tail(): uses --predicate 'process == "<unit>"' --last 5m, returns last n lines.
- recent_errors(): converts relative "N minutes ago" / "N hours ago" to --last Nm/Nh;
  passes ISO timestamps through via --start.
  The only current caller (server_scanner._check_recent_errors) passes "15 minutes ago".
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from rawos.kernel.arch.macos import MacOSLogReader


def _mock_run(stdout: str, returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


# ── tail ─────────────────────────────────────────────────────────────────────

def test_tail_returns_last_n_lines():
    reader = MacOSLogReader()
    lines = [f"line{i}" for i in range(1, 11)]
    with patch("rawos.kernel.arch.macos.subprocess.run",
               return_value=_mock_run("\n".join(lines))):
        result = reader.tail("com.example.svc", 3)
    assert result == "line8\nline9\nline10"


def test_tail_returns_all_when_output_shorter_than_n():
    reader = MacOSLogReader()
    with patch("rawos.kernel.arch.macos.subprocess.run",
               return_value=_mock_run("line1\nline2")):
        result = reader.tail("com.example.svc", 10)
    assert result == "line1\nline2"


def test_tail_returns_empty_on_nonzero_exit():
    reader = MacOSLogReader()
    with patch("rawos.kernel.arch.macos.subprocess.run",
               return_value=_mock_run("", returncode=1)):
        assert reader.tail("com.example.svc", 10) == ""


def test_tail_returns_empty_on_exception():
    reader = MacOSLogReader()
    with patch("rawos.kernel.arch.macos.subprocess.run",
               side_effect=OSError("log unavailable")):
        assert reader.tail("com.example.svc", 10) == ""


# ── recent_errors ─────────────────────────────────────────────────────────────

def test_recent_errors_uses_last_flag_for_minutes():
    """'15 minutes ago' → --last 15m (the only current caller's format)."""
    reader = MacOSLogReader()
    with patch("rawos.kernel.arch.macos.subprocess.run",
               return_value=_mock_run("some error")) as mock_run:
        reader.recent_errors("com.example.svc", "15 minutes ago")
    args = mock_run.call_args[0][0]
    assert "--last" in args
    assert "15m" in args
    assert "--start" not in args


def test_recent_errors_uses_last_flag_for_hours():
    reader = MacOSLogReader()
    with patch("rawos.kernel.arch.macos.subprocess.run",
               return_value=_mock_run("")) as mock_run:
        reader.recent_errors("com.example.svc", "2 hours ago")
    args = mock_run.call_args[0][0]
    assert "--last" in args
    assert "2h" in args


def test_recent_errors_uses_start_for_iso_timestamp():
    reader = MacOSLogReader()
    with patch("rawos.kernel.arch.macos.subprocess.run",
               return_value=_mock_run("")) as mock_run:
        reader.recent_errors("com.example.svc", "2026-01-15 10:30:00")
    args = mock_run.call_args[0][0]
    assert "--start" in args
    assert "2026-01-15 10:30:00" in args
    assert "--last" not in args


def test_recent_errors_returns_output_stripped():
    reader = MacOSLogReader()
    with patch("rawos.kernel.arch.macos.subprocess.run",
               return_value=_mock_run("  error line  \n")):
        result = reader.recent_errors("com.example.svc", "15 minutes ago")
    assert result == "error line"


def test_recent_errors_returns_empty_on_exception():
    reader = MacOSLogReader()
    with patch("rawos.kernel.arch.macos.subprocess.run",
               side_effect=OSError("log unavailable")):
        assert reader.recent_errors("com.example.svc", "15 minutes ago") == ""


def test_recent_errors_returns_empty_on_nonzero_exit():
    reader = MacOSLogReader()
    with patch("rawos.kernel.arch.macos.subprocess.run",
               return_value=_mock_run("", returncode=1)):
        assert reader.recent_errors("com.example.svc", "15 minutes ago") == ""
