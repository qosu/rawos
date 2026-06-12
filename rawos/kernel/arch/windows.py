"""kernel/arch/windows — the Windows Server arch backend.

EXPERIMENTAL: unit-tested with mocked subprocess only. Never live until a
Windows host verifies behavior. Marked EXPERIMENTAL and excluded from the
live autonomous path via supports_reversible_apply=False permanently.

Differences vs Linux (all documented, not hidden):
- ServiceManager: PowerShell Get-Service / Restart-Service (SCM) instead of
  systemctl. supports_reversible_apply=False permanently — no live canary
  is possible without a Windows host.
- LogReader: PowerShell Get-WinEvent (Windows Event Log) instead of journalctl.
  Relative since-strings converted to PowerShell AddMinutes/AddHours expressions.
- ResourceProbe: shutil.disk_usage (GetDiskFreeSpaceEx) instead of df.
- ShellPolicy: PowerShell via cmd.exe passthrough. No ulimit — Job Objects are
  the Windows resource-limit mechanism but are not implemented here.
  Documented gap: no hard resource cap on Windows in this backend.
- CrashReporter: Get-WinEvent Level=1 (Critical) from Application log.
  Level=1 = application crashes via Windows Error Reporting.
  Level=2 (Error) is used by LogReader.recent_errors() — intentionally distinct.
"""
from __future__ import annotations

import re
import shutil
import subprocess

from rawos.kernel.arch.base import ReadonlyWhitelist

EXPERIMENTAL: bool = True

_RELATIVE_MINUTE_RE = re.compile(r"^(\d+)\s+minute", re.IGNORECASE)
_RELATIVE_HOUR_RE = re.compile(r"^(\d+)\s+hour", re.IGNORECASE)


def _parse_relative_since_windows(since: str) -> str:
    """Convert 'N minutes ago' / 'N hours ago' to a PowerShell datetime expression.

    Returns the inline PowerShell expression for -FilterHashtable StartTime.
    Falls back to a quoted string for ISO timestamps and other formats.
    """
    m = _RELATIVE_MINUTE_RE.match(since)
    if m:
        return f"((Get-Date).AddMinutes(-{m.group(1)}))"
    m = _RELATIVE_HOUR_RE.match(since)
    if m:
        return f"((Get-Date).AddHours(-{m.group(1)}))"
    return f"'{since}'"


class WindowsResourceProbe:
    def disk_percent(self, path: str) -> int | None:
        try:
            usage = shutil.disk_usage(path)
        except Exception:
            return None
        if usage.total == 0:
            return None
        return int(usage.used * 100 // usage.total)


class WindowsServiceManager:
    supports_reversible_apply = False

    def list_failed(self) -> list[str]:
        """Return names of Automatic-start services currently Stopped.

        Windows SCM has no "failed" state — Stopped automatic services are the
        closest equivalent to systemd failed units (they stopped unexpectedly or
        failed to start). Running services are never returned even if they had a
        prior crash.
        """
        try:
            r = subprocess.run(
                ["powershell.exe", "-NonInteractive", "-Command",
                 "Get-Service | Where-Object {$_.StartType -eq 'Automatic' -and "
                 "$_.Status -eq 'Stopped'} | Select-Object -ExpandProperty Name"],
                capture_output=True, text=True, timeout=10.0,
            )
        except Exception:
            return []
        if r.returncode != 0:
            return []
        return [line.strip() for line in r.stdout.strip().splitlines() if line.strip()]

    def is_active(self, name: str) -> bool:
        """Return True if the named SCM service status is Running."""
        try:
            r = subprocess.run(
                ["powershell.exe", "-NonInteractive", "-Command",
                 f"(Get-Service -Name '{name}' -ErrorAction SilentlyContinue).Status"],
                capture_output=True, text=True, timeout=3.0,
            )
        except Exception:
            return False
        return r.stdout.strip() == "Running"

    def restart(self, name: str) -> bool:
        """Restart the named SCM service. True on success.

        NOTE: supports_reversible_apply=False — this is EXPERIMENTAL.
        The auto-apply path cannot reach this method (structural gate).
        """
        try:
            r = subprocess.run(
                ["powershell.exe", "-NonInteractive", "-Command",
                 f"Restart-Service -Name '{name}' -Force -ErrorAction Stop"],
                capture_output=True, text=True, timeout=30.0,
            )
        except Exception:
            return False
        return r.returncode == 0


class WindowsLogReader:
    def tail(self, unit: str, n: int) -> str:
        """Return the last n Event Log entries for the named provider."""
        try:
            r = subprocess.run(
                ["powershell.exe", "-NonInteractive", "-Command",
                 f"Get-WinEvent -LogName Application -MaxEvents {n} "
                 f"-ErrorAction SilentlyContinue | "
                 f"Where-Object {{$_.ProviderName -eq '{unit}'}} | "
                 f"Select-Object -ExpandProperty Message"],
                capture_output=True, text=True, timeout=10.0,
            )
        except Exception:
            return ""
        if r.returncode != 0:
            return ""
        return r.stdout.strip()

    def recent_errors(self, unit: str, since: str) -> str:
        """Return Error-level (Level=2) Event Log entries for the named provider.

        Relative since-strings ("15 minutes ago", "2 hours ago") are converted
        to inline PowerShell AddMinutes/AddHours expressions. ISO timestamps are
        passed as quoted strings to -FilterHashtable StartTime.
        """
        since_ps = _parse_relative_since_windows(since)
        try:
            r = subprocess.run(
                ["powershell.exe", "-NonInteractive", "-Command",
                 f"Get-WinEvent -FilterHashtable "
                 f"@{{LogName='Application'; Level=2; StartTime={since_ps}}} "
                 f"-ErrorAction SilentlyContinue | "
                 f"Where-Object {{$_.ProviderName -eq '{unit}'}} | "
                 f"Select-Object -ExpandProperty Message"],
                capture_output=True, text=True, timeout=10.0,
            )
        except Exception:
            return ""
        if r.returncode != 0:
            return ""
        return r.stdout.strip()


class WindowsCrashReporter:
    def recent_crashes(self, since: str) -> list[str]:
        """Return unique provider names with Level=1 (Critical) Application events.

        Level=1 = Critical in Windows Event Log — application crashes recorded by
        Windows Error Reporting appear here. Level=2 (Error) is used by
        WindowsLogReader.recent_errors(); Level=1 here is intentionally distinct.

        EXPERIMENTAL: never live until a Windows host verifies it.
        """
        since_ps = _parse_relative_since_windows(since)
        try:
            r = subprocess.run(
                ["powershell.exe", "-NonInteractive", "-Command",
                 f"Get-WinEvent -FilterHashtable "
                 f"@{{LogName='Application'; Level=1; StartTime={since_ps}}} "
                 f"-ErrorAction SilentlyContinue | "
                 f"Select-Object -ExpandProperty ProviderName"],
                capture_output=True, text=True, timeout=10.0,
            )
        except Exception:
            return []
        if r.returncode != 0:
            return []
        names = {line.strip() for line in r.stdout.strip().splitlines() if line.strip()}
        return sorted(names)


class WindowsShellPolicy:
    def wrap(self, command: str, workdir: str) -> tuple[str, dict]:
        """PowerShell via cmd.exe passthrough. No ulimit (Job Objects not implemented).

        cmd.exe (create_subprocess_shell's default on Windows) invokes
        powershell.exe -NonInteractive which runs the inner PowerShell script.
        Documented gap: no hard resource cap on Windows in this backend.
        """
        shell_cmd = (
            f"powershell.exe -NonInteractive -Command "
            f"\"Set-Location '{workdir}'; {command}\""
        )
        return shell_cmd, {}

    def readonly_whitelist(self) -> ReadonlyWhitelist:
        """Windows has no systemctl/journalctl; both sets are empty.

        Get-Service and Get-WinEvent are the Windows equivalents and will be
        added to _is_bash_readonly_safe when this backend goes live (future
        extension, not implemented in Stage C).
        """
        return ReadonlyWhitelist(
            systemctl_subcmds=frozenset(),
            journalctl_blocked=(),
        )
