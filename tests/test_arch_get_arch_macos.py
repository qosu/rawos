"""
get_arch() with arch_override="macos" returns a Backend with macOS implementations.

Tests run on any host (including Linux) via arch_override. The lru_cache key is
the OS enum value, not the Settings object, so LINUX and MACOS backends are cached
independently — no interference with existing Linux backend tests.
"""
from __future__ import annotations

from rawos.config import Settings
from rawos.kernel.arch import _build_backend, get_arch
from rawos.kernel.arch.macos import (
    MacOSLogReader,
    MacOSResourceProbe,
    MacOSServiceManager,
    MacOSShellPolicy,
)


def test_get_arch_macos_returns_macos_backend_classes():
    _build_backend.cache_clear()
    try:
        backend = get_arch(Settings(arch_override="macos"))
        assert isinstance(backend.resource_probe, MacOSResourceProbe)
        assert isinstance(backend.service_manager, MacOSServiceManager)
        assert isinstance(backend.log_reader, MacOSLogReader)
        assert isinstance(backend.shell_policy, MacOSShellPolicy)
    finally:
        _build_backend.cache_clear()


def test_get_arch_macos_service_manager_supports_reversible_apply_is_false():
    _build_backend.cache_clear()
    try:
        backend = get_arch(Settings(arch_override="macos"))
        assert backend.service_manager.supports_reversible_apply is False
    finally:
        _build_backend.cache_clear()
