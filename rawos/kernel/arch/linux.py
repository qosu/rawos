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


class LinuxFrontDoor:
    """Linux implementation of the FrontDoor Protocol.

    Mechanism: an sshd drop-in file at
    /etc/ssh/sshd_config.d/50-rawos-frontdoor.conf containing a
    `Match User root / ForceCommand ...` block.

    The drop-in dir keeps the main sshd_config pristine.  All mutations
    go through validate() (sshd -t) before reload(), so a broken config
    can never take effect on the live daemon.

    Backup tokens (snapshot / restore) are timestamped copies of the
    drop-in file stored alongside it; if the drop-in does not exist,
    the sentinel string "ABSENT" represents "no front-door installed".
    """

    _MANAGED_COMMENT = "# Managed by rawos — do not edit manually.\n"
    _ABSENT_SENTINEL = "ABSENT"

    def __init__(
        self,
        dropin_path: "str | Path | None" = None,
    ) -> None:
        from pathlib import Path
        self._dropin = Path(dropin_path) if dropin_path is not None else Path(
            "/etc/ssh/sshd_config.d/50-rawos-frontdoor.conf"
        )

    # ------------------------------------------------------------------
    # FrontDoor Protocol implementation
    # ------------------------------------------------------------------

    def install(self, entry_command: str) -> None:
        """Write the sshd drop-in block.

        Does NOT call validate() or reload() — the caller controls that
        (install_with_deadman orchestrates the full sequence).
        """
        content = (
            self._MANAGED_COMMENT
            + "Match User root\n"
            + f"    ForceCommand {entry_command}\n"
        )
        self._dropin.parent.mkdir(parents=True, exist_ok=True)
        self._dropin.write_text(content)

    def uninstall(self) -> None:
        """Remove the drop-in file if it exists (idempotent)."""
        try:
            self._dropin.unlink()
        except FileNotFoundError:
            pass

    def state(self) -> "FrontDoorState":
        from rawos.kernel.arch.base import FrontDoorState
        if not self._dropin.exists():
            return FrontDoorState(
                installed=False, entry_command=None, config_path=None
            )
        content = self._dropin.read_text()
        entry_command = self._parse_force_command(content)
        return FrontDoorState(
            installed=True,
            entry_command=entry_command,
            config_path=str(self._dropin),
        )

    def validate(self) -> bool:
        """Return True if `sshd -t` exits 0 (config is syntactically valid)."""
        try:
            r = subprocess.run(
                ["sshd", "-t"],
                capture_output=True, text=True, timeout=5.0,
            )
        except Exception:
            return False
        return r.returncode == 0

    def reload(self) -> None:
        """Signal sshd to reload its configuration via systemctl."""
        subprocess.run(
            ["systemctl", "reload", "ssh"],
            capture_output=True, text=True, timeout=10.0,
        )

    def snapshot(self) -> str:
        """Return an opaque restore token representing current drop-in state.

        Token is either:
        - "ABSENT" if no drop-in exists (restore will delete the drop-in), or
        - the absolute path of a timestamped backup copy.
        """
        import shutil
        import time
        if not self._dropin.exists():
            return self._ABSENT_SENTINEL
        backup = self._dropin.with_suffix(f".bak.{int(time.time() * 1000)}")
        shutil.copy2(str(self._dropin), str(backup))
        return str(backup)

    def restore(self, snapshot: str) -> None:
        """Restore the drop-in to the state captured by snapshot().

        If snapshot == "ABSENT", deletes the drop-in (restoring "no front-door").
        Otherwise copies the backup back.
        """
        import shutil
        from pathlib import Path
        if snapshot == self._ABSENT_SENTINEL:
            try:
                self._dropin.unlink()
            except FileNotFoundError:
                pass
        else:
            shutil.copy2(snapshot, str(self._dropin))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_force_command(content: str) -> "str | None":
        """Extract the ForceCommand value from a drop-in block, or None."""
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("forcecommand"):
                parts = stripped.split(None, 1)
                if len(parts) == 2:
                    return parts[1]
        return None
