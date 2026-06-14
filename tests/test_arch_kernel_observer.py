"""tests/test_arch_kernel_observer.py — TDD for the KernelObserver arch ABI Protocol (Phase 24a).

probe_command() / parse_event() are pure: no subprocess is spawned here. parse_event
is exercised against fixture bpftrace `-f json` output lines.
"""
from __future__ import annotations

import json

from rawos.kernel.arch.linux import LinuxKernelObserver
from rawos.kernel.arch.macos import MacOSKernelObserver
from rawos.kernel.arch.windows import WindowsKernelObserver


# ---------------------------------------------------------------------------
# supports_kernel_observation flag per backend
# ---------------------------------------------------------------------------

def test_supports_kernel_observation_true_on_linux():
    assert LinuxKernelObserver().supports_kernel_observation is True


def test_supports_kernel_observation_false_on_macos():
    assert MacOSKernelObserver().supports_kernel_observation is False


def test_supports_kernel_observation_false_on_windows():
    assert WindowsKernelObserver().supports_kernel_observation is False


# ---------------------------------------------------------------------------
# probe_command()
# ---------------------------------------------------------------------------

def test_probe_command_invokes_bpftrace_json():
    cmd = LinuxKernelObserver().probe_command()

    assert cmd[0] == "bpftrace"
    assert "-f" in cmd
    assert "json" in cmd
    assert "-e" in cmd


def test_probe_command_script_contains_both_probes():
    cmd = LinuxKernelObserver().probe_command()
    script = cmd[-1]

    assert "tracepoint:syscalls:sys_enter_execve" in script
    assert "kprobe:tcp_connect" in script


# ---------------------------------------------------------------------------
# parse_event()
# ---------------------------------------------------------------------------

def test_parse_event_unwraps_printf_envelope():
    observer = LinuxKernelObserver()
    inner = {"event_type": "execve", "comm": "bash", "pid": 1234, "path": "/bin/ls"}
    line = json.dumps({"type": "printf", "data": json.dumps(inner)})

    assert observer.parse_event(line) == inner


def test_parse_event_returns_none_for_non_printf_envelope():
    observer = LinuxKernelObserver()
    line = json.dumps({"type": "attached_probes", "probes": 2})

    assert observer.parse_event(line) is None


def test_parse_event_returns_none_for_malformed_outer_json():
    observer = LinuxKernelObserver()

    assert observer.parse_event("not json at all") is None


def test_parse_event_returns_none_for_malformed_inner_json():
    observer = LinuxKernelObserver()
    line = json.dumps({"type": "printf", "data": "not json {{"})

    assert observer.parse_event(line) is None


def test_parse_event_returns_none_for_non_dict_inner_payload():
    observer = LinuxKernelObserver()
    line = json.dumps({"type": "printf", "data": json.dumps([1, 2, 3])})

    assert observer.parse_event(line) is None


# ---------------------------------------------------------------------------
# Backend wiring
# ---------------------------------------------------------------------------

def test_get_arch_linux_wires_kernel_observer(monkeypatch):
    from rawos.kernel.arch import get_arch

    monkeypatch.setattr("sys.platform", "linux")
    backend = get_arch()

    assert isinstance(backend.kernel_observer, LinuxKernelObserver)
