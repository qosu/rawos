"""
kernel/arch/detect — OS enum detection for rawos's arch backend layer.

current_os() must default to the real host (sys.platform) but be
overridable via Settings.arch_override for deterministic tests on any
host (Stage A is developed/tested on Linux; Stage B will run the same
suite on macOS).
"""
from __future__ import annotations

import pytest

from rawos.kernel.arch.detect import OS, current_os


def test_current_os_returns_linux_on_linux_platform(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    assert current_os() == OS.LINUX


def test_current_os_returns_macos_on_darwin_platform(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    assert current_os() == OS.MACOS


def test_current_os_returns_windows_on_win32_platform(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    assert current_os() == OS.WINDOWS


def test_arch_override_setting_takes_precedence(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("ARCH_OVERRIDE", "macos")
    from rawos.config import Settings
    assert current_os(Settings()) == OS.MACOS


def test_unknown_platform_raises(monkeypatch):
    monkeypatch.setattr("sys.platform", "freebsd13")
    with pytest.raises(ValueError):
        current_os()
