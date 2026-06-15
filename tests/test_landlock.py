"""tests/test_landlock.py — TDD for rawos/kernel/landlock.py (Phase 26).

TDD Iron Law: this file must go RED before landlock.py is written
(ModuleNotFoundError: No module named 'rawos.kernel.landlock').

Phase 26 — Self-Imposed Kernel MAC via Landlock. The being binds its own
run_bash action surface with a kernel-enforced (LSM) ruleset that restricts
ONLY the spawned process + its descendants (structurally zero-lockout).

These tests exercise the real Landlock LSM via real syscalls (ctypes) -- there
is no mock for "the kernel denied this open()". If `landlock.supported() <
landlock.MIN_ABI`, the kernel-enforcement tests are skipped (CI/dev boxes
without Landlock in the active LSM list), but the pure-Python tests (Policy,
fail-closed validation, wrap() flag plumbing) always run.
"""
from __future__ import annotations

import dataclasses
import socket
import subprocess
import sys

import pytest

from rawos.kernel import landlock
from rawos.kernel.arch.linux import LinuxShellPolicy
from rawos.kernel.sandbox import run_bash


pytestmark = pytest.mark.skipif(
    landlock.supported() < landlock.MIN_ABI,
    reason=f"Landlock ABI {landlock.supported()} < required {landlock.MIN_ABI} "
           "(not in active LSM list / kernel too old)",
)


# ---------------------------------------------------------------------------
# ABI detection
# ---------------------------------------------------------------------------
def test_supported_returns_abi_at_least_min_on_this_box():
    assert landlock.supported() >= landlock.MIN_ABI


# ---------------------------------------------------------------------------
# Kernel-enforcement proof (unfakeable: real EACCES from the kernel)
# ---------------------------------------------------------------------------
def test_restrict_self_allows_read_within_ro_path(tmp_path):
    policy = landlock.Policy(ro_paths=("/usr",), rw_paths=(str(tmp_path),))
    restrict = landlock.build_restrict_self_fn(policy)

    result = subprocess.run(
        ["cat", "/usr/bin/true"],
        preexec_fn=restrict,
        capture_output=True,
    )

    assert result.returncode == 0


def test_restrict_self_denies_read_outside_envelope(tmp_path):
    policy = landlock.Policy(ro_paths=("/usr",), rw_paths=(str(tmp_path),))
    restrict = landlock.build_restrict_self_fn(policy)

    # /etc is NOT in this policy's ro_paths/rw_paths -> denied by the kernel.
    result = subprocess.run(
        ["cat", "/etc/hostname"],
        preexec_fn=restrict,
        capture_output=True,
    )

    assert result.returncode != 0
    assert b"Permission denied" in result.stderr


def test_pr_set_no_new_privs_set_in_child(tmp_path):
    policy = landlock.Policy(ro_paths=("/usr", "/proc"), rw_paths=(str(tmp_path),))
    restrict = landlock.build_restrict_self_fn(policy)

    result = subprocess.run(
        ["grep", "NoNewPrivs", "/proc/self/status"],
        preexec_fn=restrict,
        capture_output=True,
    )

    assert result.returncode == 0
    assert b"NoNewPrivs:\t1" in result.stdout


def test_tcp_connect_denied_outside_allowed_ports(tmp_path):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        policy = landlock.Policy(
            # /root/rawos needed for sys.executable's venv (pyvenv.cfg) to start.
            ro_paths=("/usr", "/root/rawos"), rw_paths=(str(tmp_path),),
            tcp_connect_ports=(port + 1,),  # NOT `port` -> connect to `port` denied
        )
        restrict = landlock.build_restrict_self_fn(policy)

        result = subprocess.run(
            [sys.executable, "-c",
             f"import socket; s=socket.socket(); s.settimeout(2); "
             f"s.connect(('127.0.0.1', {port}))"],
            preexec_fn=restrict,
            capture_output=True,
        )

        assert result.returncode != 0
        assert b"PermissionError" in result.stderr
    finally:
        srv.close()


def test_tcp_connect_allowed_within_envelope(tmp_path):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        policy = landlock.Policy(
            # /root/rawos needed for sys.executable's venv (pyvenv.cfg) to start.
            ro_paths=("/usr", "/root/rawos"), rw_paths=(str(tmp_path),),
            tcp_connect_ports=(port,),
        )
        restrict = landlock.build_restrict_self_fn(policy)

        result = subprocess.run(
            [sys.executable, "-c",
             f"import socket; s=socket.socket(); s.settimeout(2); "
             f"s.connect(('127.0.0.1', {port}))"],
            preexec_fn=restrict,
            capture_output=True,
        )

        assert result.returncode == 0, result.stderr
    finally:
        srv.close()


# ---------------------------------------------------------------------------
# Fail-closed: bad policy must raise in the PARENT, before any fork
# ---------------------------------------------------------------------------
def test_build_restrict_self_fn_fail_closed_on_nonexistent_path():
    policy = landlock.Policy(rw_paths=("/this/path/does/not/exist/xyz",))

    with pytest.raises(OSError):
        landlock.build_restrict_self_fn(policy)


# ---------------------------------------------------------------------------
# validate_boot_config (I-LL4 fail-fast)
# ---------------------------------------------------------------------------
def test_validate_boot_config_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(landlock, "_abi_cache", 0)
    landlock.validate_boot_config(enabled=False)  # must not raise


def test_validate_boot_config_raises_when_enabled_and_unsupported(monkeypatch):
    monkeypatch.setattr(landlock, "_abi_cache", 0)

    with pytest.raises(landlock.LandlockUnsupportedError):
        landlock.validate_boot_config(enabled=True)


def test_validate_boot_config_ok_when_enabled_and_supported(monkeypatch):
    monkeypatch.setattr(landlock, "_abi_cache", landlock.MIN_ABI)
    landlock.validate_boot_config(enabled=True)  # must not raise


# ---------------------------------------------------------------------------
# LinuxShellPolicy.wrap — flag plumbing (no behavior change when off)
# ---------------------------------------------------------------------------
def test_wrap_flag_off_is_byte_identical_to_before(monkeypatch, tmp_path):
    monkeypatch.setattr("rawos.config.settings.landlock_self_mac_enabled", False)

    shell_cmd, exec_kwargs = LinuxShellPolicy().wrap("echo hi", str(tmp_path))

    assert exec_kwargs == {}
    assert shell_cmd == (
        f"cd {str(tmp_path)!r} && ulimit -v 524288 -f 102400 -u 256 2>/dev/null; echo hi"
    )


def test_wrap_flag_on_adds_preexec_fn(monkeypatch, tmp_path):
    monkeypatch.setattr("rawos.config.settings.landlock_self_mac_enabled", True)

    shell_cmd, exec_kwargs = LinuxShellPolicy().wrap("echo hi", str(tmp_path))

    assert callable(exec_kwargs.get("preexec_fn"))


def test_wrap_flag_on_but_unsupported_falls_back_to_passthrough(monkeypatch, tmp_path):
    monkeypatch.setattr("rawos.config.settings.landlock_self_mac_enabled", True)
    monkeypatch.setattr(landlock, "_abi_cache", 0)

    shell_cmd, exec_kwargs = LinuxShellPolicy().wrap("echo hi", str(tmp_path))

    assert exec_kwargs == {}


# ---------------------------------------------------------------------------
# run_bash integration — out-of-envelope vs in-envelope, end to end
# ---------------------------------------------------------------------------
async def test_run_bash_denies_out_of_envelope_path(monkeypatch, tmp_path):
    monkeypatch.setattr("rawos.config.settings.landlock_self_mac_enabled", True)

    # /root/rawos/PLAN.md is outside DEFAULT_BEING_ENVELOPE (only
    # /root/rawos/data is RW; /root itself is not in ro_paths).
    result = await run_bash("cat /root/rawos/PLAN.md", str(tmp_path))

    assert result.exit_code != 0


async def test_run_bash_allows_in_envelope_path(monkeypatch, tmp_path):
    monkeypatch.setattr("rawos.config.settings.landlock_self_mac_enabled", True)

    # /etc/hostname is under /etc, which IS in DEFAULT_BEING_ENVELOPE.ro_paths.
    result = await run_bash("cat /etc/hostname", str(tmp_path))

    assert result.exit_code == 0


async def test_run_bash_allows_write_within_workdir(monkeypatch, tmp_path):
    monkeypatch.setattr("rawos.config.settings.landlock_self_mac_enabled", True)

    result = await run_bash("echo hello > out.txt && cat out.txt", str(tmp_path))

    assert result.exit_code == 0
    assert "hello" in result.stdout


# ---------------------------------------------------------------------------
# DEFAULT_BEING_ENVELOPE sanity
# ---------------------------------------------------------------------------
def test_default_being_envelope_is_frozen_and_composable(tmp_path):
    # arch/linux.py composes the per-call policy by adding workdir to rw_paths;
    # confirm Policy supports dataclasses.replace for this.
    extended = dataclasses.replace(
        landlock.DEFAULT_BEING_ENVELOPE,
        rw_paths=landlock.DEFAULT_BEING_ENVELOPE.rw_paths + (str(tmp_path),),
    )
    assert str(tmp_path) in extended.rw_paths
    assert str(tmp_path) not in landlock.DEFAULT_BEING_ENVELOPE.rw_paths


async def test_run_bash_allows_dev_null_redirect(monkeypatch, tmp_path):
    monkeypatch.setattr("rawos.config.settings.landlock_self_mac_enabled", True)

    # git, systemctl and many other operator commands redirect to /dev/null;
    # DEFAULT_BEING_ENVELOPE must not break this (found via standalone
    # envelope-vs-legitimate-use proof, Phase 26 verification step 3).
    result = await run_bash("echo hi > /dev/null && echo ok", str(tmp_path))

    assert result.exit_code == 0
    assert "ok" in result.stdout
