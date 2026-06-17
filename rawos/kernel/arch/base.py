"""kernel/arch/base — rawos's ABI.

These Protocols are the ONLY interface the rawos kernel uses to talk to
the host. Every arch backend (linux.py, macos.py, windows.py, and any
future backend) implements them exactly. The kernel never calls a raw
OS command directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ResourceProbe(Protocol):
    def disk_percent(self, path: str) -> int | None:
        """Return disk usage percent for `path`, or None if the probe failed."""
        ...


class ServiceManager(Protocol):
    supports_reversible_apply: bool
    supports_service_ops: bool

    def list_failed(self) -> list[str]:
        """Return unit names currently in a FAILED state."""
        ...

    def is_active(self, name: str) -> bool:
        """Return True if the named unit is active/running."""
        ...

    def restart(self, name: str) -> bool:
        """Restart the named unit. Return True on success, False on failure."""
        ...

    def start(self, name: str) -> bool:
        """Start the named unit. Return True on success, False on failure."""
        ...

    def stop(self, name: str) -> bool:
        """Stop the named unit. Return True on success, False on failure."""
        ...

    def generate_unit(
        self,
        name: str,
        exec_start: str,
        working_dir: str,
        env_file: str,
        description: str = '',
    ) -> str:
        """Return systemd unit file content as a string."""
        ...

    def install_unit(
        self,
        name: str,
        unit_content: str,
        unit_dir: str = '/etc/systemd/system',
    ) -> None:
        """Write unit file, daemon-reload, enable unit."""
        ...

    def uninstall_unit(
        self,
        name: str,
        unit_dir: str = '/etc/systemd/system',
    ) -> None:
        """Disable, stop, remove unit file, daemon-reload."""
        ...


class LogReader(Protocol):
    def tail(self, unit: str, n: int) -> str:
        """Return the last `n` log lines for `unit`, or "" on failure."""
        ...

    def recent_errors(self, unit: str, since: str) -> str:
        """Return error-level log output for `unit` since `since`, or "" on failure/none."""
        ...


@dataclass(frozen=True)
class ReadonlyWhitelist:
    """Arch-specific subsets of the diagnostic read-only shell whitelist."""

    systemctl_subcmds: frozenset[str]
    journalctl_blocked: tuple[str, ...]


class ShellPolicy(Protocol):
    def wrap(self, command: str, workdir: str) -> tuple[str, dict]:
        """Return (shell_cmd, exec_kwargs): the resource-limited shell command
        to execute (with cd/ulimit prefix applied) and any extra kwargs for
        the subprocess launcher."""
        ...

    def readonly_whitelist(self) -> ReadonlyWhitelist:
        """Return the arch-specific systemctl/journalctl read-only whitelist subsets."""
        ...


class CrashReporter(Protocol):
    def recent_crashes(self, since: str) -> list[str]:
        """Return sorted unique process names that crashed since `since`.

        `since` accepts "N minutes ago", "N hours ago", or ISO 8601 timestamp.
        Returns [] on failure, permission error, or no desktop crash context.
        """
        ...


@dataclass(frozen=True)
class FrontDoorState:
    """State snapshot of the front-door installation on a host."""

    installed: bool
    entry_command: str | None
    config_path: str | None


class FrontDoor(Protocol):
    """OS-specific mechanism for making an interactive login invoke rawos.

    The kernel uses only this Protocol; every arch backend implements it.
    install() must validate the configuration before persisting (never write
    a config that sshd -t rejects). reload() is called separately so the
    caller controls exactly when the change takes effect.
    """

    def install(self, entry_command: str) -> None:
        """Configure the host so that an interactive login invokes entry_command."""
        ...

    def uninstall(self) -> None:
        """Remove the front-door configuration; restore the prior login behavior."""
        ...

    def state(self) -> FrontDoorState:
        """Return the current front-door installation state."""
        ...

    def validate(self) -> bool:
        """Return True if the pending/active config passes a syntactic check (e.g. sshd -t)."""
        ...

    def reload(self) -> None:
        """Signal the login service to reload its configuration."""
        ...

    def snapshot(self) -> str:
        """Return an opaque token representing the current config state for revert."""
        ...

    def restore(self, snapshot: str) -> None:
        """Roll back the config to the state captured by snapshot()."""
        ...


@dataclass(frozen=True)
class FileSnapshot:
    """Captured pre-state of a file for FileOperator.backup/restore.

    `existed=False, content=None` represents "the file did not exist" —
    restore() of such a snapshot deletes the file (back to absent).
    """

    path: str
    existed: bool
    content: bytes | None


class FileOperatorRefusalError(Exception):
    """Raised when a FileOperator refuses to write/restore a self-protected path.

    Self-protected paths (the rawos unit file, the §5 front-door sshd config,
    and the rawos source tree itself — which also covers the operator
    allowlist DB under it) can never be mutated through this Protocol, even
    if an owner allowlist entry would otherwise permit it. read()/exists()/
    backup() remain available (R0, read-only).
    """


class FileOperator(Protocol):
    """OS-specific mechanism for the operator to read/write/snapshot real
    machine files, operating on absolute paths.

    The kernel uses only this Protocol for operator file edits — never
    kernel.sandbox.run_bash. write()/restore() raise FileOperatorRefusalError
    for self-protected paths (see FileOperatorRefusalError).
    """

    supports_file_ops: bool

    def read(self, path: str) -> bytes | None:
        """Return the file's contents, or None if it does not exist."""
        ...

    def write(self, path: str, content: bytes) -> None:
        """Write `content` to `path`, creating parent directories as needed.

        Raises FileOperatorRefusalError for self-protected paths.
        """
        ...

    def exists(self, path: str) -> bool:
        """Return True if `path` exists."""
        ...

    def backup(self, path: str) -> FileSnapshot:
        """Capture the current state of `path` for later restore()."""
        ...

    def restore(self, snapshot: FileSnapshot) -> None:
        """Restore `snapshot.path` to the state captured by backup().

        If the snapshot represents "absent", the file is deleted.
        Raises FileOperatorRefusalError for self-protected paths.
        """
        ...



class UnitTopologyManager(Protocol):
    """Protocol for systemd unit/boot topology management (Phase 23-full).

    Implementations wrap systemctl/systemd-analyze as the stable ABI.
    NOT in Backend dataclass — mirrors LinuxKernelEnforcer pattern (dormant stub).
    """

    def author_unit(self, unit_name: str, content: str) -> None:
        """Write unit_name with given content to /etc/systemd/system/."""
        ...

    def delete_unit(self, unit_name: str) -> None:
        """Remove unit_name from /etc/systemd/system/ (no-op if absent)."""
        ...

    def read_unit(self, unit_name: str) -> "str | None":
        """Return unit file content, or None if the file does not exist."""
        ...

    def enable(self, unit_name: str) -> None:
        """systemctl enable unit_name."""
        ...

    def disable(self, unit_name: str) -> None:
        """systemctl disable unit_name."""
        ...

    def is_enabled(self, unit_name: str) -> bool:
        """Return True iff systemctl is-enabled reports 'enabled'."""
        ...

    def set_default(self, target: str) -> None:
        """systemctl set-default target."""
        ...

    def get_default(self) -> str:
        """systemctl get-default."""
        ...

    def daemon_reload(self) -> None:
        """systemctl daemon-reload."""
        ...

    def analyze_verify(self) -> "tuple[bool, str]":
        """Run systemd-analyze verify across all units in /etc/systemd/system/.

        Returns (ok, output_text). ok=True iff exit code 0.
        """
        ...

    def is_active(self, unit_name: str) -> bool:
        """Return True iff systemctl is-active reports active."""
        ...

    def is_system_running(self) -> bool:
        """Return True iff systemctl is-system-running is not failed/stopping."""
        ...

    def list_dependencies(self, *unit_names: str) -> str:
        """Return stdout of systemctl list-dependencies --all <unit_names>."""
        ...


class KernelObserver(Protocol):
    """OS-specific mechanism for the being's kernel-level perception (Phase 24a).

    Read-only, machine-wide: probe_command() returns the argv of a subprocess
    that streams kernel events (process execution, outbound network connections)
    as JSON lines on stdout — observe only, never gates or denies anything.
    parse_event() normalizes one stdout line into an event dict, or returns
    None for non-event/malformed lines. Must never raise.
    """

    supports_kernel_observation: bool

    def probe_command(self) -> list[str]:
        """Return the argv of a subprocess that streams kernel events as JSON lines."""
        ...

    def parse_event(self, line: str) -> dict | None:
        """Parse one stdout line into a normalized event dict, or None if the
        line carries no perception event (control message or malformed)."""
        ...
        ...
