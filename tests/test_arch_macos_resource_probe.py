"""
MacOSResourceProbe — uses shutil.disk_usage instead of df.

Linux backend keeps df --output=pcent because it accounts for reserved blocks
(~5% on ext4) which shutil.disk_usage does not. macOS has no --output=pcent
flag for df, so shutil.disk_usage is the correct macOS approach — the semantic
gap (reserve accounting) is stated in the plan, not hidden.
"""
from __future__ import annotations

from collections import namedtuple
from unittest.mock import patch

from rawos.kernel.arch.macos import MacOSResourceProbe

DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])


def test_disk_percent_returns_used_percent():
    probe = MacOSResourceProbe()
    with patch("rawos.kernel.arch.macos.shutil.disk_usage",
               return_value=DiskUsage(total=200, used=150, free=50)):
        result = probe.disk_percent("/")
    assert result == 75


def test_disk_percent_rounds_down():
    probe = MacOSResourceProbe()
    with patch("rawos.kernel.arch.macos.shutil.disk_usage",
               return_value=DiskUsage(total=100, used=33, free=67)):
        result = probe.disk_percent("/")
    assert result == 33


def test_disk_percent_returns_none_on_exception():
    probe = MacOSResourceProbe()
    with patch("rawos.kernel.arch.macos.shutil.disk_usage",
               side_effect=OSError("no such file")):
        assert probe.disk_percent("/nonexistent") is None


def test_disk_percent_returns_none_on_zero_total():
    probe = MacOSResourceProbe()
    with patch("rawos.kernel.arch.macos.shutil.disk_usage",
               return_value=DiskUsage(total=0, used=0, free=0)):
        assert probe.disk_percent("/") is None
