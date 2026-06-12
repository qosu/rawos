"""
MacOSShellPolicy — PosixShellPolicy without ulimit -v.

Darwin does not support the -v (virtual memory) ulimit flag.
This is a documented semantic gap: no hard address-space cap on macOS.
Keeps -f (file size) and -u (max user processes) which ARE supported.

readonly_whitelist(): macOS has no systemctl/journalctl; both sets empty.
"""
from __future__ import annotations

from rawos.kernel.arch.macos import MacOSShellPolicy


def test_wrap_excludes_ulimit_v():
    policy = MacOSShellPolicy()
    cmd, _ = policy.wrap("pytest", "/tmp/workdir")
    assert "-v" not in cmd


def test_wrap_includes_ulimit_f_and_u():
    policy = MacOSShellPolicy()
    cmd, _ = policy.wrap("pytest", "/tmp/workdir")
    assert "-f" in cmd
    assert "-u" in cmd


def test_wrap_includes_cd_workdir():
    policy = MacOSShellPolicy()
    cmd, _ = policy.wrap("pytest", "/tmp/workdir")
    assert "cd '/tmp/workdir'" in cmd or 'cd "/tmp/workdir"' in cmd


def test_wrap_appends_command():
    policy = MacOSShellPolicy()
    cmd, _ = policy.wrap("pytest tests/", "/tmp/workdir")
    assert "pytest tests/" in cmd


def test_wrap_returns_empty_kwargs():
    policy = MacOSShellPolicy()
    _, kwargs = policy.wrap("pytest", "/tmp/workdir")
    assert kwargs == {}


def test_readonly_whitelist_systemctl_subcmds_empty():
    """No systemctl on macOS — empty set so all systemctl calls are rejected."""
    policy = MacOSShellPolicy()
    wl = policy.readonly_whitelist()
    assert wl.systemctl_subcmds == frozenset()


def test_readonly_whitelist_journalctl_blocked_empty():
    """No journalctl on macOS — empty tuple."""
    policy = MacOSShellPolicy()
    wl = policy.readonly_whitelist()
    assert wl.journalctl_blocked == ()
