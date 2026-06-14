"""kernel/arch/macos — the macOS arch backend.

Differences vs Linux (all documented, not hidden):
- ResourceProbe: shutil.disk_usage instead of df --output=pcent. Linux keeps df
  because it accounts for root-reserved blocks (~5% on ext4); macOS df lacks
  --output=pcent, so shutil (statvfs) is the correct macOS approach.
- ServiceManager: launchctl instead of systemctl.
  supports_reversible_apply=False until a live canary test verifies launchctl
  kickstart atomic restart behavior (Stage B criterion, not yet met).
- LogReader: macOS Unified Log (log show) instead of journalctl.
  Relative since-strings ("N minutes ago") are converted to --last Nm/Nh.
- ShellPolicy: ulimit without -v. Darwin does not support the virtual-memory
  cap flag (-v). Semantic gap: no hard address-space limit on macOS.
- CrashReporter: scans /Library/Logs/DiagnosticReports for .crash/.ips files
  by mtime. Process name is filename stem split on '_', index 0.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from rawos.kernel.arch.base import ReadonlyWhitelist

_DIAGNOSTIC_REPORTS_DIR = Path("/Library/Logs/DiagnosticReports")

_RELATIVE_MINUTE_RE = re.compile(r"^(\d+)\s+minute", re.IGNORECASE)
_RELATIVE_HOUR_RE = re.compile(r"^(\d+)\s+hour", re.IGNORECASE)


def _parse_relative_since(since: str) -> str | None:
    """Convert 'N minutes ago' / 'N hours ago' to log --last value (Nm / Nh).

    Returns None for unrecognized formats; caller falls back to --start <since>.
    """
    m = _RELATIVE_MINUTE_RE.match(since)
    if m:
        return f"{m.group(1)}m"
    m = _RELATIVE_HOUR_RE.match(since)
    if m:
        return f"{m.group(1)}h"
    return None


class MacOSResourceProbe:
    def disk_percent(self, path: str) -> int | None:
        try:
            usage = shutil.disk_usage(path)
        except Exception:
            return None
        if usage.total == 0:
            return None
        return int(usage.used * 100 // usage.total)


class MacOSServiceManager:
    supports_reversible_apply = False
    supports_service_ops = False

    def list_failed(self) -> list[str]:
        """Return launchd labels with non-zero exit status and no running PID.

        launchctl list columns: PID \\t Status \\t Label.
        A service is "failed" when PID is "-" (not running) and Status != 0.
        Services currently running with a non-zero previous exit code are excluded.
        """
        try:
            r = subprocess.run(
                ["launchctl", "list"],
                capture_output=True, text=True, timeout=5.0,
            )
        except Exception:
            return []
        if r.returncode != 0:
            return []
        failed = []
        for line in r.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            pid_col, status_col, label_col = parts
            if pid_col.strip() != "-":
                continue
            try:
                status = int(status_col.strip())
            except ValueError:
                continue
            if status != 0:
                failed.append(label_col.strip())
        return failed

    def is_active(self, name: str) -> bool:
        """Return True if the named launchd label is loaded and has a running PID.

        launchctl list <label> exits non-zero when the label is not loaded.
        When loaded and running, the output contains a '"PID"' key.
        """
        try:
            r = subprocess.run(
                ["launchctl", "list", name],
                capture_output=True, text=True, timeout=3.0,
            )
        except Exception:
            return False
        if r.returncode != 0:
            return False
        return '"PID"' in r.stdout

    def restart(self, name: str) -> bool:
        """Kickstart with kill flag in the system domain. True on success.

        NOTE: supports_reversible_apply=False — this is for manual use only.
        The auto-apply path cannot reach this method (structural gate).
        """
        try:
            r = subprocess.run(
                ["launchctl", "kickstart", "-k", f"system/{name}"],
                capture_output=True, text=True, timeout=30.0,
            )
        except Exception:
            return False
        return r.returncode == 0

    def start(self, name: str) -> bool:
        """NOTE: supports_service_ops=False — never reached by the operator gate."""
        return False

    def stop(self, name: str) -> bool:
        """NOTE: supports_service_ops=False — never reached by the operator gate."""
        return False


class MacOSLogReader:
    def tail(self, unit: str, n: int) -> str:
        """Return the last n log lines for the named process via Unified Log."""
        try:
            r = subprocess.run(
                ["log", "show", "--style", "syslog",
                 "--predicate", f'process == "{unit}"',
                 "--last", "5m"],
                capture_output=True, text=True, timeout=10.0,
            )
        except Exception:
            return ""
        if r.returncode != 0:
            return ""
        lines = r.stdout.strip().splitlines()
        if n < len(lines):
            return "\n".join(lines[-n:])
        return r.stdout.strip()

    def recent_errors(self, unit: str, since: str) -> str:
        """Return error-level log output for the named process since `since`.

        Relative strings ("15 minutes ago", "2 hours ago") are converted to
        --last Nm/Nh. ISO timestamps are passed through via --start.
        """
        last_flag = _parse_relative_since(since)
        predicate = f'process == "{unit}" AND messageType == "error"'
        try:
            if last_flag:
                args = ["log", "show", "--style", "syslog",
                        "--predicate", predicate, "--last", last_flag]
            else:
                args = ["log", "show", "--style", "syslog",
                        "--predicate", predicate, "--start", since]
            r = subprocess.run(args, capture_output=True, text=True, timeout=10.0)
        except Exception:
            return ""
        if r.returncode != 0:
            return ""
        return r.stdout.strip()


def _parse_since_to_datetime(since: str) -> datetime | None:
    """Convert 'N minutes ago' / 'N hours ago' / ISO 8601 → datetime.

    Used by MacOSCrashReporter to compare against file mtime. Returns None
    if `since` cannot be parsed — caller should return [] in that case.
    """
    m = _RELATIVE_MINUTE_RE.match(since)
    if m:
        return datetime.now() - timedelta(minutes=int(m.group(1)))
    m = _RELATIVE_HOUR_RE.match(since)
    if m:
        return datetime.now() - timedelta(hours=int(m.group(1)))
    try:
        return datetime.fromisoformat(since)
    except ValueError:
        return None


class MacOSCrashReporter:
    def recent_crashes(self, since: str) -> list[str]:
        """Return sorted unique process names with crash reports newer than `since`.

        Scans /Library/Logs/DiagnosticReports for .crash and .ips files whose
        mtime is after the parsed `since` datetime. Process name is the filename
        stem split on '_', index 0 (macOS format: {name}_{date}_{host}.crash).
        Returns [] on OSError (e.g. permission denied) or unparseable `since`.
        """
        since_dt = _parse_since_to_datetime(since)
        if since_dt is None:
            return []
        try:
            names: set[str] = set()
            for entry in _DIAGNOSTIC_REPORTS_DIR.iterdir():
                if entry.suffix not in (".crash", ".ips"):
                    continue
                try:
                    mtime = datetime.fromtimestamp(entry.stat().st_mtime)
                except OSError:
                    continue
                if mtime < since_dt:
                    continue
                names.add(entry.name.split("_")[0])
            return sorted(names)
        except OSError:
            return []


class MacOSShellPolicy:
    def wrap(self, command: str, workdir: str) -> tuple[str, dict]:
        """ulimit without -v (Darwin does not support virtual-memory cap).

        Keeps -f (file size blocks) and -u (max user processes).
        """
        shell_cmd = (
            f"cd {workdir!r} && "
            "ulimit -f 102400 -u 256 2>/dev/null; "
            + command
        )
        return shell_cmd, {}

    def readonly_whitelist(self) -> ReadonlyWhitelist:
        """macOS has no systemctl/journalctl; both sets are empty."""
        return ReadonlyWhitelist(
            systemctl_subcmds=frozenset(),
            journalctl_blocked=(),
        )


class MacOSFrontDoor:
    """macOS front-door backend — NOT YET IMPLEMENTED.

    Documented limitation: macOS uses sshd differently from Linux (launchd
    instead of systemd; no drop-in config dir). The sshd_config mechanism
    works but needs path adjustments and launchctl reload instead of
    systemctl. Deferred until a macOS host is available for live canary.

    The Protocol seam exists; this stub makes the gap explicit rather than
    hidden.
    """

    def install(self, entry_command: str) -> None:
        raise NotImplementedError("front-door backend not yet implemented for macOS")

    def uninstall(self) -> None:
        raise NotImplementedError("front-door backend not yet implemented for macOS")

    def state(self):
        raise NotImplementedError("front-door backend not yet implemented for macOS")

    def validate(self) -> bool:
        raise NotImplementedError("front-door backend not yet implemented for macOS")

    def reload(self) -> None:
        raise NotImplementedError("front-door backend not yet implemented for macOS")

    def snapshot(self) -> str:
        raise NotImplementedError("front-door backend not yet implemented for macOS")

    def restore(self, snapshot: str) -> None:
        raise NotImplementedError("front-door backend not yet implemented for macOS")



class MacOSFileOperator:
    """macOS file-operator backend — NOT YET IMPLEMENTED.

    supports_file_ops = False: the operator path is disabled on macOS
    (kernel.operator checks this flag before attempting any file edit).
    The Protocol seam exists; this stub makes the gap explicit rather than
    hidden, mirroring MacOSFrontDoor.
    """

    supports_file_ops = False

    def read(self, path: str) -> bytes | None:
        raise NotImplementedError("file operator backend not yet implemented for macOS")

    def write(self, path: str, content: bytes) -> None:
        raise NotImplementedError("file operator backend not yet implemented for macOS")

    def exists(self, path: str) -> bool:
        raise NotImplementedError("file operator backend not yet implemented for macOS")

    def backup(self, path: str):
        raise NotImplementedError("file operator backend not yet implemented for macOS")

    def restore(self, snapshot) -> None:
        raise NotImplementedError("file operator backend not yet implemented for macOS")


class MacOSKernelObserver:
    """macOS kernel-observer backend — NOT YET IMPLEMENTED.

    supports_kernel_observation = False: the Phase 24a perception loop checks
    this flag and no-ops on macOS. The Protocol seam exists; this stub makes
    the gap explicit rather than hidden, mirroring MacOSFileOperator.
    """

    supports_kernel_observation = False

    def probe_command(self) -> list[str]:
        raise NotImplementedError("kernel observer backend not yet implemented for macOS")

    def parse_event(self, line: str) -> dict | None:
        raise NotImplementedError("kernel observer backend not yet implemented for macOS")
