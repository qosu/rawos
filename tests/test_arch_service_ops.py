"""tests/test_arch_service_ops.py — TDD for ServiceManager.start/stop (Phase 23a, Step 1).

Extends the ServiceManager Protocol with supports_service_ops, start(), stop().
LinuxServiceManager: supports_service_ops=True, start/stop via systemctl
(subprocess mocked here — live verification happens on the box separately).
macOS/Windows: supports_service_ops=False, start/stop are inert stubs returning
False (never reached by the operator gate, mirrors supports_reversible_apply=False).
"""
from __future__ import annotations

import subprocess

import pytest

from rawos.kernel.arch.linux import LinuxServiceManager
from rawos.kernel.arch.macos import MacOSServiceManager
from rawos.kernel.arch.windows import WindowsServiceManager


class TestLinuxServiceManagerStartStop:
    def setup_method(self):
        self.mgr = LinuxServiceManager()

    def test_supports_service_ops_true(self):
        assert self.mgr.supports_service_ops is True

    def test_start_invokes_systemctl_start_and_returns_true_on_success(self, monkeypatch):
        captured = {}

        def _fake_run(args, **kwargs):
            captured["args"] = args
            return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        assert self.mgr.start("rawos-svcprobe.service") is True
        assert captured["args"] == ["systemctl", "start", "rawos-svcprobe.service"]

    def test_start_returns_false_on_nonzero_exit(self, monkeypatch):
        def _fake_run(args, **kwargs):
            return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        assert self.mgr.start("rawos-svcprobe.service") is False

    def test_start_returns_false_on_exception(self, monkeypatch):
        def _raise(*args, **kwargs):
            raise OSError("systemctl not found")

        monkeypatch.setattr(subprocess, "run", _raise)

        assert self.mgr.start("rawos-svcprobe.service") is False

    def test_stop_invokes_systemctl_stop_and_returns_true_on_success(self, monkeypatch):
        captured = {}

        def _fake_run(args, **kwargs):
            captured["args"] = args
            return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        assert self.mgr.stop("rawos-svcprobe.service") is True
        assert captured["args"] == ["systemctl", "stop", "rawos-svcprobe.service"]

    def test_stop_returns_false_on_nonzero_exit(self, monkeypatch):
        def _fake_run(args, **kwargs):
            return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="boom")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        assert self.mgr.stop("rawos-svcprobe.service") is False


class TestMacOSServiceManagerStartStop:
    def test_supports_service_ops_false(self):
        assert MacOSServiceManager().supports_service_ops is False

    def test_start_is_inert_stub(self):
        assert MacOSServiceManager().start("anything") is False

    def test_stop_is_inert_stub(self):
        assert MacOSServiceManager().stop("anything") is False


class TestWindowsServiceManagerStartStop:
    def test_supports_service_ops_false(self):
        assert WindowsServiceManager().supports_service_ops is False

    def test_start_is_inert_stub(self):
        assert WindowsServiceManager().start("anything") is False

    def test_stop_is_inert_stub(self):
        assert WindowsServiceManager().stop("anything") is False
