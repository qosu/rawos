"""rawos/kernel/landlock.py — Phase 26: self-imposed kernel MAC via Landlock.

The being voluntarily binds its own free-form-shell action surface
(`rawos.kernel.sandbox.run_bash`) with a kernel-enforced (LSM) access-control
ruleset. Unlike every other rawos safety mechanism, this is NOT Python checking
itself -- the kernel refuses denied syscalls (open/connect) for the sandboxed
process and ALL of its descendants, unconditionally, with no Python code in the
enforcement path.

Structurally zero-lockout: Landlock restricts only the calling process + its
descendants (never sshd, never the rawos main process itself, never the host).
A wrong policy can only break the being's OWN sandboxed commands -- it can never
lock out the operator or the box. This is why Landlock (unlike eBPF-LSM
machine-wide or PID1 authority) is buildable and provable without a reboot and
without an out-of-band recovery plan.

Reference: linux/landlock.h (kernel >= 5.13). Pure ctypes -- no compiled
extension, no clang/libbpf dependency.

Lifecycle (see kernel/arch/linux.py::LinuxShellPolicy.wrap):
  PARENT (rawos main process, before fork):
    - build_restrict_self_fn(policy) opens a ruleset fd, attaches every
      path-beneath / net-port rule declared by `policy`, and returns a closure
      capturing that fd.
  CHILD (between fork and exec, via subprocess preexec_fn):
    - the closure calls prctl(PR_SET_NO_NEW_PRIVS) + landlock_restrict_self():
      exactly 2 syscalls, no allocation on the success path.
  PARENT (after the child has been spawned):
    - closes its own copy of the ruleset fd (see closure._ruleset_fd, consumed
      by sandbox.py) -- the child's copy was already consumed by
      landlock_restrict_self and is dropped at exec (O_CLOEXEC).

Fail-closed: any error while building the ruleset (bad path, syscall failure)
raises immediately in the PARENT, before any fork -- the command is never run
"unsandboxed by accident". Fail-fast: if Landlock is disabled-but-requested at
boot, validate_boot_config() raises during lifespan startup.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
from dataclasses import dataclass
from typing import Callable

# ---------------------------------------------------------------------------
# libc / raw syscalls
#
# glibc has no wrapper for the landlock_* syscalls (too new for most libc
# versions still in wide use), so they are issued via syscall(2) directly.
# Syscall numbers 444/445/446 are stable across x86_64 and aarch64 (assigned
# from the post-2019 unified asm-generic table). prctl(2) DOES have a glibc
# wrapper and is called normally.
# ---------------------------------------------------------------------------
_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
_libc.syscall.restype = ctypes.c_long
_libc.prctl.restype = ctypes.c_int
_libc.prctl.argtypes = [
    ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
]

_SYS_landlock_create_ruleset = 444
_SYS_landlock_add_rule = 445
_SYS_landlock_restrict_self = 446

_PR_SET_NO_NEW_PRIVS = 38

_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0

_LANDLOCK_RULE_PATH_BENEATH = 1
_LANDLOCK_RULE_NET_PORT = 2

# --- LANDLOCK_ACCESS_FS_* (linux/landlock.h) --------------------------------
_ACCESS_FS_EXECUTE = 1 << 0
_ACCESS_FS_WRITE_FILE = 1 << 1
_ACCESS_FS_READ_FILE = 1 << 2
_ACCESS_FS_READ_DIR = 1 << 3
_ACCESS_FS_REMOVE_DIR = 1 << 4
_ACCESS_FS_REMOVE_FILE = 1 << 5
_ACCESS_FS_MAKE_CHAR = 1 << 6
_ACCESS_FS_MAKE_DIR = 1 << 7
_ACCESS_FS_MAKE_REG = 1 << 8
_ACCESS_FS_MAKE_SOCK = 1 << 9
_ACCESS_FS_MAKE_FIFO = 1 << 10
_ACCESS_FS_MAKE_BLOCK = 1 << 11
_ACCESS_FS_MAKE_SYM = 1 << 12
_ACCESS_FS_REFER = 1 << 13          # ABI >= 2
_ACCESS_FS_TRUNCATE = 1 << 14       # ABI >= 3

_ACCESS_FS_RO = _ACCESS_FS_EXECUTE | _ACCESS_FS_READ_FILE | _ACCESS_FS_READ_DIR

# --- LANDLOCK_ACCESS_NET_* (ABI >= 4) ---------------------------------------
_ACCESS_NET_BIND_TCP = 1 << 0
_ACCESS_NET_CONNECT_TCP = 1 << 1


def _fs_access_mask(abi: int) -> int:
    """Every LANDLOCK_ACCESS_FS_* bit supported by `abi`, ABI1 baseline up."""
    mask = (1 << 13) - 1  # ABI1: EXECUTE .. MAKE_SYM (bits 0-12)
    if abi >= 2:
        mask |= _ACCESS_FS_REFER
    if abi >= 3:
        mask |= _ACCESS_FS_TRUNCATE
    return mask


# ---------------------------------------------------------------------------
# kernel struct layouts (linux/landlock.h)
# ---------------------------------------------------------------------------
class _landlock_ruleset_attr(ctypes.Structure):
    _fields_ = [
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
    ]


class _landlock_path_beneath_attr(ctypes.Structure):
    # Kernel struct is __attribute__((packed)): 8 + 4 = 12 bytes, no padding.
    _pack_ = 1
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


class _landlock_net_port_attr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("port", ctypes.c_uint64),
    ]


def _syscall(nr: int, *args: int | ctypes._Pointer) -> int:
    """Issue syscall `nr`. Every plain-int arg is widened to c_long (64-bit
    register width on x86_64/aarch64 -- correct for fds, flags, sizes AND for
    a 0 used as a NULL pointer). ctypes pointer args (byref(...)) pass through.
    """
    ctypes.set_errno(0)
    wrapped: list[object] = []
    for a in args:
        wrapped.append(ctypes.c_long(a) if isinstance(a, int) else a)
    return _libc.syscall(ctypes.c_long(nr), *wrapped)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class LandlockError(Exception):
    """Building or attaching a Landlock ruleset failed (fail-closed: the
    command this would have guarded is never executed)."""


class LandlockUnsupportedError(LandlockError):
    """Landlock ABI on this kernel is below MIN_ABI (see validate_boot_config)."""


# Network enforcement (LANDLOCK_ACCESS_NET_*) requires ABI 4. The being's
# envelope is defined in terms of both filesystem AND network rules together --
# silently providing FS-only enforcement on an older kernel would look like
# "the sandbox is active" while actually being weaker than declared. Fail-fast
# instead (validate_boot_config) rather than fail-quiet.
MIN_ABI = 4


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Policy:
    """A Landlock envelope.

    ro_paths -- read + execute + list-directory only (LANDLOCK_ACCESS_FS_RO).
    rw_paths -- full read/write/create/remove/execute (every FS access bit
                the running kernel's ABI supports).
    tcp_connect_ports -- outbound TCP connect allowed to these ports
                         (loopback included automatically by the kernel rule
                         scope -- Landlock net rules are not address-scoped).
    tcp_bind_ports -- inbound TCP bind allowed to these ports.

    Any path NOT covered by ro_paths/rw_paths is fully denied (handled_access_fs
    covers all FS access bits). Any TCP connect/bind to a port NOT listed is
    fully denied (handled_access_net covers both bits, ABI >= 4 required).
    """

    ro_paths: tuple[str, ...] = ()
    rw_paths: tuple[str, ...] = ()
    tcp_connect_ports: tuple[int, ...] = ()
    tcp_bind_ports: tuple[int, ...] = ()


# The being's default action-surface envelope for run_bash. Deliberately a
# static superset (no per-action-class policy in v1 -- see Phase 26 plan,
# "stated limitations"). Tuned to not break ordinary operator commands (git,
# systemctl status, file read/write in workdir) while denying everything else
# by kernel default: other users' homes, ~/.ssh of a human operator, arbitrary
# egress ports.
DEFAULT_BEING_ENVELOPE = Policy(
    # /proc (RO): systemctl/ps/many tools read /proc/* (boot id, self, etc).
    ro_paths=("/usr", "/bin", "/lib", "/lib64", "/sbin", "/etc", "/proc"),
    # /dev (RW): /dev/null, /dev/zero, /dev/urandom etc are essential -- git,
    # systemctl and most coreutils open /dev/null for read+write routinely.
    rw_paths=("/tmp", "/var/tmp", "/root/rawos/data", "/dev"),
    tcp_connect_ports=(53, 80, 443),
    tcp_bind_ports=(),
)


# ---------------------------------------------------------------------------
# ABI detection
# ---------------------------------------------------------------------------
_abi_cache: int | None = None


def supported() -> int:
    """Return the Landlock ABI version supported by the running kernel, or 0
    if Landlock is unavailable (ENOSYS/EOPNOTSUPP, e.g. CI containers, kernels
    < 5.13, or `landlock` not in the active LSM list). Cached -- the kernel
    cannot change ABI version while running."""
    global _abi_cache
    if _abi_cache is None:
        ret = _syscall(_SYS_landlock_create_ruleset, 0, 0, _LANDLOCK_CREATE_RULESET_VERSION)
        _abi_cache = ret if ret > 0 else 0
    return _abi_cache


def validate_boot_config(*, enabled: bool) -> None:
    """Called once from api/app.py::lifespan (I-LL4, fail-fast).

    If Landlock self-MAC is enabled but this kernel's ABI is below MIN_ABI,
    raise immediately at boot rather than silently booting with
    `landlock_self_mac_enabled=True` while every run_bash call is, in fact,
    unsandboxed. Forces the operator to fix config explicitly instead of
    rawos lying about its own security posture.
    """
    if not enabled:
        return
    abi = supported()
    if abi < MIN_ABI:
        raise LandlockUnsupportedError(
            f"landlock_self_mac_enabled=True but Landlock ABI {abi} < required "
            f"{MIN_ABI} (kernel too old, or 'landlock' not in the active LSM "
            f"list -- check /sys/kernel/security/lsm)."
        )


# ---------------------------------------------------------------------------
# Ruleset construction (PARENT, pre-fork) + restrict closure (CHILD, pre-exec)
# ---------------------------------------------------------------------------
def _add_path_rule(ruleset_fd: int, path: str, allowed_access: int) -> None:
    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        rule = _landlock_path_beneath_attr(allowed_access=allowed_access, parent_fd=fd)
        rc = _syscall(_SYS_landlock_add_rule, ruleset_fd, _LANDLOCK_RULE_PATH_BENEATH,
                       ctypes.byref(rule), 0)
        if rc != 0:
            raise LandlockError(
                f"landlock_add_rule(path_beneath, {path!r}) failed: "
                f"errno={ctypes.get_errno()}"
            )
    finally:
        os.close(fd)


def _add_net_rule(ruleset_fd: int, port: int, allowed_access: int) -> None:
    rule = _landlock_net_port_attr(allowed_access=allowed_access, port=port)
    rc = _syscall(_SYS_landlock_add_rule, ruleset_fd, _LANDLOCK_RULE_NET_PORT,
                   ctypes.byref(rule), 0)
    if rc != 0:
        raise LandlockError(
            f"landlock_add_rule(net_port, {port}) failed: errno={ctypes.get_errno()}"
        )


def build_restrict_self_fn(policy: Policy) -> Callable[[], None]:
    """PARENT-side (pre-fork): build a Landlock ruleset fd from `policy` and
    return a closure that, when called as a subprocess `preexec_fn` (CHILD,
    post-fork pre-exec), applies it via landlock_restrict_self.

    Fail-closed: any failure while building the ruleset raises here, in the
    PARENT, before any fork -- the caller's command is never launched.

    The returned closure carries the open ruleset fd as `._ruleset_fd` so the
    PARENT can close its now-redundant copy after the child has been spawned
    (see kernel/sandbox.py::run_bash). The fd is O_CLOEXEC, so the CHILD's
    copy is dropped automatically at exec, after landlock_restrict_self has
    consumed it.
    """
    abi = supported()
    if abi < MIN_ABI:
        raise LandlockUnsupportedError(f"Landlock ABI {abi} < required {MIN_ABI}")

    attr = _landlock_ruleset_attr(
        handled_access_fs=_fs_access_mask(abi),
        handled_access_net=_ACCESS_NET_BIND_TCP | _ACCESS_NET_CONNECT_TCP,
    )
    ruleset_fd = _syscall(_SYS_landlock_create_ruleset, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if ruleset_fd < 0:
        raise LandlockError(f"landlock_create_ruleset failed: errno={ctypes.get_errno()}")

    try:
        for path in policy.ro_paths:
            _add_path_rule(ruleset_fd, path, _ACCESS_FS_RO)
        for path in policy.rw_paths:
            _add_path_rule(ruleset_fd, path, _fs_access_mask(abi))
        for port in policy.tcp_connect_ports:
            _add_net_rule(ruleset_fd, port, _ACCESS_NET_CONNECT_TCP)
        for port in policy.tcp_bind_ports:
            _add_net_rule(ruleset_fd, port, _ACCESS_NET_BIND_TCP)
    except Exception:
        os.close(ruleset_fd)
        raise

    def _restrict() -> None:
        # CHILD, between fork() and exec(). Async-signal-safe: exactly 2
        # syscalls, no allocation, no logging, no lock on the success path.
        # A raise here propagates to the PARENT via subprocess's child-error
        # pipe (the same mechanism used for exec() failures) -- fail-closed:
        # the command is never exec'd unsandboxed.
        rc = _libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
        if rc != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS) failed")
        rc = _syscall(_SYS_landlock_restrict_self, ruleset_fd, 0)
        if rc != 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")

    _restrict._ruleset_fd = ruleset_fd  # type: ignore[attr-defined]
    return _restrict
