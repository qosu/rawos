"""kernel/arch/linux — the Linux arch backend.

Today's complete backend build. Reproduces, byte-for-byte, the
commands previously inlined in context/server_scanner.py and
kernel/sandbox.py — Stage A is a zero-behavior-change extraction.
"""
from __future__ import annotations

import subprocess

from rawos.kernel.arch.base import ReadonlyWhitelist


class LinuxResourceProbe:
    def disk_percent(self, path: str) -> int | None:
        try:
            r = subprocess.run(
                ["df", path, "--output=pcent"],
                capture_output=True, text=True, timeout=3.0,
            )
        except Exception:
            return None
        if r.returncode != 0:
            return None
        try:
            pct_str = r.stdout.strip().splitlines()[-1].strip().rstrip("%")
            return int(pct_str)
        except (IndexError, ValueError):
            return None


class LinuxServiceManager:
    supports_reversible_apply = True

    def list_failed(self) -> list[str]:
        try:
            r = subprocess.run(
                ["systemctl", "list-units", "--type=service", "--state=failed",
                 "--no-legend", "--no-pager", "--plain"],
                capture_output=True, text=True, timeout=5.0,
            )
        except Exception:
            return []
        if r.returncode != 0 or not r.stdout.strip():
            return []

        units = []
        for line in r.stdout.strip().splitlines():
            parts = line.split()
            if not parts:
                continue
            if len(parts) >= 2 and parts[1] == "not-found":
                continue
            units.append(parts[0].rstrip())
        return units

    def is_active(self, name: str) -> bool:
        try:
            r = subprocess.run(
                ["systemctl", "is-active", name],
                capture_output=True, text=True, timeout=3.0,
            )
        except Exception:
            return False
        return r.stdout.strip() == "active"

    def restart(self, name: str) -> bool:
        try:
            r = subprocess.run(
                ["systemctl", "restart", name],
                capture_output=True, text=True, timeout=30.0,
            )
        except Exception:
            return False
        return r.returncode == 0


class LinuxLogReader:
    def tail(self, unit: str, n: int) -> str:
        try:
            r = subprocess.run(
                ["journalctl", "-u", unit, "-n", str(n), "--no-pager", "-q",
                 "--output=short-monotonic"],
                capture_output=True, text=True, timeout=3.0,
            )
        except Exception:
            return ""
        if r.returncode != 0:
            return ""
        return r.stdout.strip()

    def recent_errors(self, unit: str, since: str) -> str:
        try:
            r = subprocess.run(
                ["journalctl", "-u", unit, "--since", since,
                 "-p", "err", "-q", "--no-pager", "--output=short"],
                capture_output=True, text=True, timeout=3.0,
            )
        except Exception:
            return ""
        if r.returncode != 0:
            return ""
        return r.stdout.strip()


class LinuxCrashReporter:
    def recent_crashes(self, since: str) -> list[str]:
        """Stub: server Linux has no desktop crash reporter context.

        Core dumps at /var/crash/ exist but are process-level artifacts, not
        managed by a platform crash reporter. Desktop anomaly detection for Linux
        is deferred — not in scope for the server-focused arch layer.
        """
        return []


class LinuxShellPolicy:
    def wrap(self, command: str, workdir: str) -> tuple[str, dict]:
        shell_cmd = (
            f"cd {workdir!r} && "
            "ulimit -v 524288 -f 102400 -u 256 2>/dev/null; "
            + command
        )
        return shell_cmd, {}

    def readonly_whitelist(self) -> ReadonlyWhitelist:
        return ReadonlyWhitelist(
            systemctl_subcmds=frozenset({
                "status", "show", "cat", "is-active", "is-failed", "is-enabled",
                "list-units", "list-unit-files", "list-timers",
            }),
            journalctl_blocked=(
                "-f", "--follow", "--flush", "--rotate", "--sync", "--relinquish-var",
            ),
        )
