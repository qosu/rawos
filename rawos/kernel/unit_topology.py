"""rawos/kernel/unit_topology.py — Phase 23-full: unit/boot topology authorship.

Design: mechanism/policy separation (mirror 24B BPF LSM pattern).
  Mechanism = systemd as PID1/engine; `systemctl`/`systemd-analyze` = stable ABI.
  Policy    = unit file content + dependency edges + enable-state + default-target.
  Floor     = compute_floor_closure() output — refuse at construction (I-UT3).

Two-fact decoupling (I-UT1/I-UT2):
  Fact A — unit file on disk (inert; 0 behavior until daemon-reload + start|enable).
  Fact B-runtime — daemon-reload + start/restart (reversible in-band, no reboot).
  Fact B-boot   — enable/disable/set-default (next-boot only, reboot-class).

Floor closure: FLOOR_UNIT_SEED + transitive deps from systemctl list-dependencies.
Default-target allowlist: _ALLOWED_DEFAULT_TARGETS (I-UT4).
Boot-graph ops: propose-only forever (I-UT7).
Dormant on ship: operator_unit_topology_enabled=False (I-UT11).
"""
from __future__ import annotations

import dataclasses
import os
import re
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Module-level caches (monkeypatchable by tests — mirror bpf_lsm pattern)
# ---------------------------------------------------------------------------

_support_cache: bool | None = None

# ---------------------------------------------------------------------------
# Floor seed — extended at runtime via compute_floor_closure()
# ---------------------------------------------------------------------------

FLOOR_UNIT_SEED: frozenset[str] = frozenset({
    # SSH / remote access
    "sshd.service", "sshd",
    "ssh.service", "ssh",
    # Init / PID1
    "systemd", "init",
    # Network
    "systemd-networkd.service", "systemd-networkd",
    "NetworkManager.service", "NetworkManager",
    "network.target", "network",
    "network-online.target", "network-online",
    # rawos itself
    "rawos.service", "rawos",
    # rawos holder/revert units (24B + Phase 23/26)
    "rawos-bpf-lsm-holder.service", "rawos-bpf-lsm-holder",
    "rawos-bpf-lsm-revert.service", "rawos-bpf-lsm-revert",
    "rawos-pam-revert.service", "rawos-pam-revert",
    "rawos-unit-topology-revert.service", "rawos-unit-topology-revert",
    "rawos-venv-revert.service", "rawos-venv-revert",
    # Login / getty / PAM
    "systemd-logind.service", "systemd-logind",
    "getty@.service", "getty@tty1.service",
    "getty@",
    # Frontdoor backstop (I-UT12)
    "rawos-frontdoor.service", "rawos-frontdoor",
    # Core systemd targets
    "basic.target", "sysinit.target", "multi-user.target",
    "default.target", "graphical.target",
    # Core systemd services
    "dbus.service", "dbus",
    "systemd-journald.service", "systemd-journald",
})

# ---------------------------------------------------------------------------
# Default-target allowlist (I-UT4)
# ---------------------------------------------------------------------------

_ALLOWED_DEFAULT_TARGETS: frozenset[str] = frozenset({
    "multi-user.target",
    "graphical.target",
})

# ---------------------------------------------------------------------------
# Operation sets (I-UT7)
# ---------------------------------------------------------------------------

_BOOT_GRAPH_OPS: frozenset[str] = frozenset({"enable", "disable", "set_default"})
_RUNTIME_OPS: frozenset[str] = frozenset({"author", "delete"})
_ALL_OPS: frozenset[str] = _BOOT_GRAPH_OPS | _RUNTIME_OPS

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UnitTopologyError(Exception):
    """Base error for unit topology operations."""


class UnitTopologyUnsupportedError(UnitTopologyError):
    """Raised when systemd is not available as PID1."""


class UnitTopologyRefusalError(UnitTopologyError):
    """Raised when a unit or target is in the floor closure and must not be touched."""


# ---------------------------------------------------------------------------
# Floor closure
# ---------------------------------------------------------------------------

# Tree-drawing characters used by systemctl list-dependencies output.
_TREE_CHARS_RE = re.compile(r"^[\s●├─└│●├─└│•]+")
# Unit name pattern: word chars, @, :, \\, /, _, - followed by .suffix
_UNIT_NAME_RE = re.compile(r"^([\w@:\\/._-]+\.[a-z]+)")


def _strip_tree_prefix(line: str) -> str:
    """Strip leading tree-drawing characters from a list-dependencies line."""
    return _TREE_CHARS_RE.sub("", line).strip()


def compute_floor_closure(list_deps_output: str) -> frozenset[str]:
    """Compute the floor closure: FLOOR_UNIT_SEED + transitive deps parsed from output.

    `list_deps_output` is the stdout of `systemctl list-dependencies --all <seeds>`.
    Passing an empty string returns seed-only closure (used by tests and static checks).

    Always returns a frozenset. Both the dotted form (foo.service) and bare form (foo)
    are included for every unit parsed.
    """
    result: set[str] = set(FLOOR_UNIT_SEED)
    for line in list_deps_output.splitlines():
        clean = _strip_tree_prefix(line)
        m = _UNIT_NAME_RE.match(clean)
        if m:
            unit = m.group(1)
            result.add(unit)
            # Add bare name (strip last .suffix) for forms like "sshd.service" → "sshd"
            dot_pos = unit.rfind(".")
            if dot_pos > 0:
                result.add(unit[:dot_pos])
    # Ensure all seed bare names present (seed may have .service + bare already)
    extra: set[str] = set()
    for u in result:
        dot_pos = u.rfind(".")
        if dot_pos > 0:
            extra.add(u[:dot_pos])
    result.update(extra)
    return frozenset(result)


def is_floor_protected(unit_name: str, floor: frozenset[str]) -> bool:
    """Return True if unit_name (or its bare form) is in the floor closure."""
    if unit_name in floor:
        return True
    # Check bare name (foo.service → foo)
    dot_pos = unit_name.rfind(".")
    if dot_pos > 0:
        bare = unit_name[:dot_pos]
        if bare in floor:
            return True
    # Check dotted form (foo → foo.service)
    if "." not in unit_name and f"{unit_name}.service" in floor:
        return True
    return False


# ---------------------------------------------------------------------------
# Supported / validate_boot_config (monkeypatchable)
# ---------------------------------------------------------------------------


def supported() -> bool:
    """Return True if systemd is available as PID1 on this host."""
    global _support_cache
    if _support_cache is not None:
        return _support_cache
    try:
        result = subprocess.run(
            ["systemctl", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        _support_cache = result.returncode == 0 and os.path.isdir("/run/systemd")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        _support_cache = False
    return _support_cache


def _get_default_target() -> str:
    """Return current systemd default.target. Monkeypatchable by tests."""
    result = subprocess.run(
        ["systemctl", "get-default"],
        capture_output=True, text=True, timeout=5,
    )
    return result.stdout.strip()


def validate_boot_config(*, enabled: bool) -> None:
    """Fail-fast check at rawos boot (I-UT9).

    enabled=False → no-op (I-UT11: dormant on ship, never raises).
    enabled=True, systemd not supported → UnitTopologyUnsupportedError.
    enabled=True, default.target not in _ALLOWED_DEFAULT_TARGETS → UnitTopologyError.
    """
    if not enabled:
        return

    if not supported():
        raise UnitTopologyUnsupportedError(
            "unit_topology: systemd not available as PID1 — "
            "cannot enable unit topology authorship on this host"
        )

    default_target = _get_default_target()
    if default_target not in _ALLOWED_DEFAULT_TARGETS:
        raise UnitTopologyError(
            f"unit_topology: current default.target={default_target!r} is not in "
            f"allowed set {sorted(_ALLOWED_DEFAULT_TARGETS)} — "
            "refuse to enable unit topology authorship until target is safe"
        )


# ---------------------------------------------------------------------------
# UnitSnapshot — captured prior state for restore()
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class UnitSnapshot:
    """Prior state captured by ReversibleUnitTopologyAction.capture()."""

    unit_name: str
    prior_content: str | None       # None = unit did not exist
    prior_enabled: bool
    prior_default_target: str


# ---------------------------------------------------------------------------
# ReversibleUnitTopologyAction — mirrors ReversibleServiceAction
# ---------------------------------------------------------------------------


class ReversibleUnitTopologyAction:
    """Reversible unit/boot-topology operation (I-UT3/I-UT4/I-UT6).

    Construction raises:
      UnitTopologyError          — unknown op, or 'author' without content.
      UnitTopologyRefusalError   — unit or target in floor closure (I-UT3),
                                    or set_default target not in allowlist (I-UT4).

    capture/apply/verify/restore implement the ReversibleOperation protocol.
    """

    def __init__(
        self,
        mgr: object,
        unit_name: str,
        op: str,
        floor: frozenset[str],
        *,
        unit_content: str | None = None,
        target_name: str | None = None,
    ) -> None:
        # --- Op validity ---
        if op not in _ALL_OPS:
            raise UnitTopologyError(
                f"Unknown unit topology op {op!r}. "
                f"Valid ops: {sorted(_ALL_OPS)}"
            )

        # --- Floor / allowlist guard ---
        if op == "set_default":
            # For set_default: unit_name is the target string; guard via allowlist (I-UT4).
            effective_target = target_name or unit_name
            if effective_target not in _ALLOWED_DEFAULT_TARGETS:
                raise UnitTopologyRefusalError(
                    f"set_default: target {effective_target!r} not in allowed set "
                    f"{sorted(_ALLOWED_DEFAULT_TARGETS)} (I-UT4)"
                )
            self._effective_target = effective_target
        else:
            # For all other ops: floor closure guard (I-UT3).
            if is_floor_protected(unit_name, floor):
                raise UnitTopologyRefusalError(
                    f"Unit {unit_name!r} is in the floor closure and must not "
                    "be touched by unit topology authorship (I-UT3)"
                )
            self._effective_target = unit_name

        # --- author requires content ---
        if op == "author":
            if not unit_content:
                raise UnitTopologyError(
                    "op='author' requires non-empty unit_content"
                )

        self._mgr = mgr
        self.unit_name: str = unit_name
        self.op: str = op
        self._floor = floor
        self._unit_content = unit_content

    # ------------------------------------------------------------------

    def capture(self) -> UnitSnapshot:
        """Snapshot prior state of the unit/default-target before any apply."""
        prior_content = self._mgr.read_unit(self.unit_name)  # type: ignore[union-attr]
        prior_enabled = self._mgr.is_enabled(self.unit_name)  # type: ignore[union-attr]
        prior_default_target = self._mgr.get_default()  # type: ignore[union-attr]
        return UnitSnapshot(
            unit_name=self.unit_name,
            prior_content=prior_content,
            prior_enabled=prior_enabled,
            prior_default_target=prior_default_target,
        )

    def apply(self) -> None:
        """Apply the operation.

        Runtime ops (author, delete) call daemon_reload after mutation (I-UT1).
        Boot-graph ops (enable, disable, set_default) do NOT daemon-reload (I-UT2).
        """
        if self.op == "author":
            self._mgr.author_unit(self.unit_name, self._unit_content)  # type: ignore[union-attr]
            self._mgr.daemon_reload()  # type: ignore[union-attr]
        elif self.op == "delete":
            self._mgr.delete_unit(self.unit_name)  # type: ignore[union-attr]
            self._mgr.daemon_reload()  # type: ignore[union-attr]
        elif self.op == "enable":
            self._mgr.enable(self.unit_name)  # type: ignore[union-attr]
            # No daemon_reload — boot-graph op (I-UT2)
        elif self.op == "disable":
            self._mgr.disable(self.unit_name)  # type: ignore[union-attr]
            # No daemon_reload — boot-graph op (I-UT2)
        elif self.op == "set_default":
            self._mgr.set_default(self._effective_target)  # type: ignore[union-attr]
            # No daemon_reload — boot-graph op (I-UT2)

    def verify(self) -> bool:
        """Run systemd-analyze verify across full config. Return True iff clean."""
        ok, _output = self._mgr.analyze_verify()  # type: ignore[union-attr]
        return ok

    def restore(self, snapshot: UnitSnapshot) -> None:
        """Restore prior state from snapshot (I-UT6 reversibility floor).

        author/delete: restore file content (or delete if unit was new) + daemon_reload.
        enable: if unit was not enabled → disable.
        disable: if unit was enabled → enable.
        set_default: restore prior default target.
        """
        if self.op in ("author", "delete"):
            if snapshot.prior_content is None:
                # Unit did not exist — delete to restore
                self._mgr.delete_unit(self.unit_name)  # type: ignore[union-attr]
            else:
                self._mgr.author_unit(self.unit_name, snapshot.prior_content)  # type: ignore[union-attr]
            self._mgr.daemon_reload()  # type: ignore[union-attr]
        elif self.op == "enable":
            if not snapshot.prior_enabled:
                self._mgr.disable(self.unit_name)  # type: ignore[union-attr]
        elif self.op == "disable":
            if snapshot.prior_enabled:
                self._mgr.enable(self.unit_name)  # type: ignore[union-attr]
        elif self.op == "set_default":
            self._mgr.set_default(snapshot.prior_default_target)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Boot-deadman arm / disarm — pure functions (I-UT8)
# Emit the shell command but never execute it (execution = human window step).
# ---------------------------------------------------------------------------

_REVERT_UNIT_NAME = "rawos-unit-topology-revert"


def arm_boot_deadman(
    delay_s: int,
    revert_cmd: str,
    unit_name: str = _REVERT_UNIT_NAME,
) -> list[str]:
    """Return the systemd-run command to arm a boot-deadman transient timer.

    Pure function — never executes. The returned list is the argv to run
    (after human review in a maintenance window, I-UT8).

    Example: arm_boot_deadman(300, "systemctl reboot") →
      ["systemd-run", "--on-active", "300", "--unit=rawos-unit-topology-revert",
       "--", "systemctl", "reboot"]
    """
    return [
        "systemd-run",
        "--on-active", str(delay_s),
        f"--unit={unit_name}",
        "--",
        *revert_cmd.split(),
    ]


def disarm_boot_deadman(unit_name: str = _REVERT_UNIT_NAME) -> list[str]:
    """Return the systemctl command to disarm (stop) the boot-deadman transient.

    Pure function — never executes.
    """
    return ["systemctl", "stop", "--no-block", unit_name]
