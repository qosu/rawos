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

    def list_failed(self) -> list[str]:
        """Return unit names currently in a FAILED state."""
        ...

    def is_active(self, name: str) -> bool:
        """Return True if the named unit is active/running."""
        ...

    def restart(self, name: str) -> bool:
        """Restart the named unit. Return True on success, False on failure."""
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
