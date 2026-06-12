"""get_arch() returns a Backend with CrashReporter wired for all three backends.

Verifies that the 5th ABI Protocol (CrashReporter) is present and correctly
typed on every backend returned by get_arch(). arch_override used to test all
three without a platform constraint.
"""
from __future__ import annotations

from rawos.config import Settings
from rawos.kernel.arch import _build_backend, get_arch
from rawos.kernel.arch.linux import LinuxCrashReporter
from rawos.kernel.arch.macos import MacOSCrashReporter
from rawos.kernel.arch.windows import WindowsCrashReporter


def test_get_arch_linux_has_linux_crash_reporter():
    _build_backend.cache_clear()
    try:
        backend = get_arch(Settings(arch_override="linux"))
        assert isinstance(backend.crash_reporter, LinuxCrashReporter)
    finally:
        _build_backend.cache_clear()


def test_get_arch_macos_has_macos_crash_reporter():
    _build_backend.cache_clear()
    try:
        backend = get_arch(Settings(arch_override="macos"))
        assert isinstance(backend.crash_reporter, MacOSCrashReporter)
    finally:
        _build_backend.cache_clear()


def test_get_arch_windows_has_windows_crash_reporter():
    _build_backend.cache_clear()
    try:
        backend = get_arch(Settings(arch_override="windows"))
        assert isinstance(backend.crash_reporter, WindowsCrashReporter)
    finally:
        _build_backend.cache_clear()
