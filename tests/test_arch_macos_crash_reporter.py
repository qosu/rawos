"""MacOSCrashReporter — scans /Library/Logs/DiagnosticReports for .crash/.ips files.

Design:
- Accepts: "N minutes ago", "N hours ago", ISO 8601 timestamp as `since`.
- Scans _DIAGNOSTIC_REPORTS_DIR for entries with suffix .crash or .ips.
- Filters by mtime > parsed `since` datetime.
- Extracts process name as filename-stem split on "_", index 0.
- Returns sorted unique names. [] on OSError or unparseable `since`.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from rawos.kernel.arch.macos import MacOSCrashReporter


def _entry(name: str, seconds_ago: float) -> MagicMock:
    """Build a mock DirEntry with given filename and mtime relative to now."""
    e = MagicMock()
    e.name = name
    e.suffix = Path(name).suffix
    stat = MagicMock()
    stat.st_mtime = (datetime.now() - timedelta(seconds=seconds_ago)).timestamp()
    e.stat.return_value = stat
    return e


# ── basic contract ────────────────────────────────────────────────────────────

def test_recent_crashes_returns_empty_when_no_files():
    reporter = MacOSCrashReporter()
    with patch("rawos.kernel.arch.macos._DIAGNOSTIC_REPORTS_DIR") as mock_dir:
        mock_dir.iterdir.return_value = []
        assert reporter.recent_crashes("15 minutes ago") == []


def test_recent_crashes_includes_crash_files_newer_than_since():
    reporter = MacOSCrashReporter()
    e = _entry("Safari_2026-06-11-103045_host.crash", seconds_ago=60)  # 1 min ago
    with patch("rawos.kernel.arch.macos._DIAGNOSTIC_REPORTS_DIR") as mock_dir:
        mock_dir.iterdir.return_value = [e]
        result = reporter.recent_crashes("15 minutes ago")
    assert "Safari" in result


def test_recent_crashes_excludes_files_older_than_since():
    reporter = MacOSCrashReporter()
    e = _entry("OldApp_2026-06-11-080000_host.crash", seconds_ago=3600)  # 1 hr ago
    with patch("rawos.kernel.arch.macos._DIAGNOSTIC_REPORTS_DIR") as mock_dir:
        mock_dir.iterdir.return_value = [e]
        result = reporter.recent_crashes("15 minutes ago")
    assert result == []


def test_recent_crashes_includes_ips_files():
    reporter = MacOSCrashReporter()
    e = _entry("Finder_2026-06-11-103045_host.ips", seconds_ago=60)
    with patch("rawos.kernel.arch.macos._DIAGNOSTIC_REPORTS_DIR") as mock_dir:
        mock_dir.iterdir.return_value = [e]
        result = reporter.recent_crashes("15 minutes ago")
    assert "Finder" in result


def test_recent_crashes_excludes_non_crash_extensions():
    reporter = MacOSCrashReporter()
    e = _entry("debug.log", seconds_ago=60)
    with patch("rawos.kernel.arch.macos._DIAGNOSTIC_REPORTS_DIR") as mock_dir:
        mock_dir.iterdir.return_value = [e]
        assert reporter.recent_crashes("15 minutes ago") == []


def test_recent_crashes_deduplicates_process_names():
    reporter = MacOSCrashReporter()
    entries = [
        _entry("Safari_2026-06-11-103045_host.crash", seconds_ago=60),
        _entry("Safari_2026-06-11-103100_host.crash", seconds_ago=30),
    ]
    with patch("rawos.kernel.arch.macos._DIAGNOSTIC_REPORTS_DIR") as mock_dir:
        mock_dir.iterdir.return_value = entries
        result = reporter.recent_crashes("15 minutes ago")
    assert result.count("Safari") == 1


def test_recent_crashes_returns_sorted():
    reporter = MacOSCrashReporter()
    entries = [
        _entry("Finder_2026-06-11-103045_host.crash", seconds_ago=60),
        _entry("App_2026-06-11-103045_host.crash", seconds_ago=60),
    ]
    with patch("rawos.kernel.arch.macos._DIAGNOSTIC_REPORTS_DIR") as mock_dir:
        mock_dir.iterdir.return_value = entries
        result = reporter.recent_crashes("15 minutes ago")
    assert result == sorted(result)


# ── since format variants ─────────────────────────────────────────────────────

def test_recent_crashes_accepts_hours_since():
    reporter = MacOSCrashReporter()
    # File is 30 minutes old, since is "2 hours ago" → should be included
    e = _entry("Dock_2026-06-11-090000_host.crash", seconds_ago=1800)
    with patch("rawos.kernel.arch.macos._DIAGNOSTIC_REPORTS_DIR") as mock_dir:
        mock_dir.iterdir.return_value = [e]
        result = reporter.recent_crashes("2 hours ago")
    assert "Dock" in result


def test_recent_crashes_accepts_iso_timestamp():
    reporter = MacOSCrashReporter()
    # mtime is 1 minute ago; since is 5 minutes ago ISO → should be included
    since_iso = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    e = _entry("Notes_2026-06-11-103045_host.crash", seconds_ago=60)
    with patch("rawos.kernel.arch.macos._DIAGNOSTIC_REPORTS_DIR") as mock_dir:
        mock_dir.iterdir.return_value = [e]
        result = reporter.recent_crashes(since_iso)
    assert "Notes" in result


def test_recent_crashes_returns_empty_for_unparseable_since():
    reporter = MacOSCrashReporter()
    result = reporter.recent_crashes("not a valid since string at all xyz")
    assert result == []


# ── error resilience ──────────────────────────────────────────────────────────

def test_recent_crashes_returns_empty_on_oserror_from_iterdir():
    reporter = MacOSCrashReporter()
    with patch("rawos.kernel.arch.macos._DIAGNOSTIC_REPORTS_DIR") as mock_dir:
        mock_dir.iterdir.side_effect = OSError("permission denied")
        assert reporter.recent_crashes("15 minutes ago") == []


def test_recent_crashes_skips_entry_when_stat_raises():
    """stat() failure on one entry does not abort — silently skips that entry."""
    reporter = MacOSCrashReporter()
    bad = MagicMock()
    bad.name = "BadApp_2026-06-11-103045_host.crash"
    bad.suffix = ".crash"
    bad.stat.side_effect = OSError("stat failed")
    good = _entry("GoodApp_2026-06-11-103045_host.crash", seconds_ago=60)
    with patch("rawos.kernel.arch.macos._DIAGNOSTIC_REPORTS_DIR") as mock_dir:
        mock_dir.iterdir.return_value = [bad, good]
        result = reporter.recent_crashes("15 minutes ago")
    assert "GoodApp" in result
    assert "BadApp" not in result
