"""kernel/arch/linux — the Linux arch backend.

Today's complete backend build. Reproduces, byte-for-byte, the
commands previously inlined in context/server_scanner.py and
kernel/sandbox.py — Stage A is a zero-behavior-change extraction.
"""
from __future__ import annotations

import subprocess


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
        pct_str = r.stdout.strip().splitlines()[-1].strip().rstrip("%")
        return int(pct_str)


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

    def restart(self, name: str) -> None:
        subprocess.run(
            ["systemctl", "restart", name],
            capture_output=True, text=True, timeout=30.0,
        )


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
