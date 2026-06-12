"""
kernel/arch.get_arch() — cached Backend bundle, the kernel's single
entry point into the ABI (kernel/arch/base.py Protocols).
"""
from __future__ import annotations

from rawos.kernel.arch import get_arch
from rawos.kernel.arch.linux import (
    LinuxLogReader,
    LinuxResourceProbe,
    LinuxServiceManager,
    LinuxShellPolicy,
)


def test_get_arch_linux_returns_linux_resource_probe(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    backend = get_arch()
    assert isinstance(backend.resource_probe, LinuxResourceProbe)


def test_get_arch_linux_returns_linux_service_manager(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    backend = get_arch()
    assert isinstance(backend.service_manager, LinuxServiceManager)


def test_get_arch_linux_returns_linux_log_reader(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    backend = get_arch()
    assert isinstance(backend.log_reader, LinuxLogReader)


def test_get_arch_linux_returns_linux_shell_policy(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    backend = get_arch()
    assert isinstance(backend.shell_policy, LinuxShellPolicy)


def test_get_arch_is_cached_singleton_per_os(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    assert get_arch() is get_arch()
