"""LinuxCrashReporter — documented stub: server Linux has no desktop crash context."""
from __future__ import annotations

from rawos.kernel.arch.linux import LinuxCrashReporter


def test_recent_crashes_returns_empty_list():
    assert LinuxCrashReporter().recent_crashes("15 minutes ago") == []


def test_recent_crashes_returns_list_type():
    result = LinuxCrashReporter().recent_crashes("2026-01-01 00:00:00")
    assert isinstance(result, list)


def test_recent_crashes_ignores_since_format():
    """Stub ignores `since` entirely — always returns []."""
    assert LinuxCrashReporter().recent_crashes("garbage input xyz") == []
