"""
kernel/arch.get_arch() — cached Backend bundle, the kernel's single
entry point into the ABI (kernel/arch/base.py Protocols).
"""
from __future__ import annotations

import pytest

from rawos.kernel.arch import get_arch
from rawos.kernel.arch.linux import LinuxResourceProbe


def test_get_arch_linux_returns_linux_resource_probe(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    backend = get_arch()
    assert isinstance(backend.resource_probe, LinuxResourceProbe)


def test_get_arch_is_cached_singleton_per_os(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    assert get_arch() is get_arch()


def test_get_arch_macos_not_yet_implemented(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("ARCH_OVERRIDE", "macos")
    from rawos.config import Settings
    with pytest.raises(NotImplementedError):
        get_arch(Settings())
