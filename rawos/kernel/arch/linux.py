"""kernel/arch/linux — the Linux arch backend.

Today's complete backend build. Reproduces, byte-for-byte, the
commands previously inlined in context/server_scanner.py and
kernel/sandbox.py — Stage A is a zero-behavior-change extraction.
"""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from pathlib import Path

from rawos.config import settings
from rawos.kernel import landlock
from rawos.kernel.arch.base import FileOperatorRefusalError, FileSnapshot, ReadonlyWhitelist


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
    supports_service_ops = True

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

    def start(self, name: str) -> bool:
        try:
            r = subprocess.run(
                ["systemctl", "start", name],
                capture_output=True, text=True, timeout=30.0,
            )
        except Exception:
            return False
        return r.returncode == 0

    def stop(self, name: str) -> bool:
        try:
            r = subprocess.run(
                ["systemctl", "stop", name],
                capture_output=True, text=True, timeout=30.0,
            )
        except Exception:
            return False
        return r.returncode == 0


    def generate_unit(
        self,
        name: str,
        exec_start: str,
        working_dir: str,
        env_file: str,
        description: str = '',
    ) -> str:
        if not description:
            description = f'{name} service'
        lines = [
            '[Unit]',
            f'Description={description}',
            'After=network.target',
            '[Service]',
            'Type=simple',
            'User=root',
            f'WorkingDirectory={working_dir}',
            f'EnvironmentFile={env_file}',
            f'ExecStart={exec_start}',
            'Restart=always',
            'RestartSec=5',
            'StandardOutput=journal',
            'StandardError=journal',
            f'SyslogIdentifier={name}',
            '[Install]',
            'WantedBy=multi-user.target',
        ]
        return '\n'.join(lines) + '\n'

    def install_unit(
        self,
        name: str,
        unit_content: str,
        unit_dir: str = '/etc/systemd/system',
    ) -> None:
        import os as _os
        unit_path = _os.path.join(unit_dir, f'{name}.service')
        with open(unit_path, 'w') as fh:
            fh.write(unit_content)
        subprocess.run(['systemctl', 'daemon-reload'], capture_output=True, text=True, timeout=15.0)
        subprocess.run(['systemctl', 'enable', name], capture_output=True, text=True, timeout=10.0)

    def uninstall_unit(
        self,
        name: str,
        unit_dir: str = '/etc/systemd/system',
    ) -> None:
        import os as _os
        subprocess.run(['systemctl', 'disable', name], capture_output=True, text=True, timeout=10.0)
        subprocess.run(['systemctl', 'stop', name], capture_output=True, text=True, timeout=15.0)
        unit_path = _os.path.join(unit_dir, f'{name}.service')
        if _os.path.isfile(unit_path):
            _os.unlink(unit_path)
        subprocess.run(['systemctl', 'daemon-reload'], capture_output=True, text=True, timeout=15.0)


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

        # Phase 26 -- Landlock self-MAC (I-LL3: dormant by default --
        # byte-for-byte old behavior when disabled or unsupported).
        if (
            settings.landlock_self_mac_enabled
            and landlock.supported() >= landlock.MIN_ABI
        ):
            policy = dataclasses.replace(
                landlock.DEFAULT_BEING_ENVELOPE,
                rw_paths=landlock.DEFAULT_BEING_ENVELOPE.rw_paths + (workdir,),
            )
            return shell_cmd, {"preexec_fn": landlock.build_restrict_self_fn(policy)}

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
            import shutil as _shutil
            _sshd = _shutil.which("sshd") or "/usr/sbin/sshd"
            r = subprocess.run(
                [_sshd, "-t"],
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



_RAWOS_UNIT_FILE = "/etc/systemd/system/rawos.service"
_RAWOS_FRONTDOOR_SSHD_CONFIG = "/etc/ssh/sshd_config.d/50-rawos-frontdoor.conf"


class LinuxFileOperator:
    """Linux implementation of the FileOperator Protocol.

    Operates on absolute paths via plain filesystem calls. write()/restore()
    refuse self-protected paths: the rawos systemd unit, the front-door sshd
    drop-in (arch/linux.py LinuxFrontDoor), and anything under rawos's own
    source tree (settings.rawos_source_root — this also covers the operator
    allowlist DB at <rawos_source_root>/data/rawos.db, so no separate special
    case is needed). read()/exists()/backup() are unrestricted (R0).
    """

    supports_file_ops = True

    def read(self, path: str) -> bytes | None:
        try:
            return Path(path).read_bytes()
        except FileNotFoundError:
            return None

    def write(self, path: str, content: bytes) -> None:
        self._refuse_if_protected(path)
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)

    def exists(self, path: str) -> bool:
        return Path(path).exists()

    def backup(self, path: str) -> FileSnapshot:
        content = self.read(path)
        if content is None:
            return FileSnapshot(path=path, existed=False, content=None)
        return FileSnapshot(path=path, existed=True, content=content)

    def restore(self, snapshot: FileSnapshot) -> None:
        self._refuse_if_protected(snapshot.path)
        if snapshot.existed:
            p = Path(snapshot.path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(snapshot.content)
        else:
            try:
                Path(snapshot.path).unlink()
            except FileNotFoundError:
                pass

    def _refuse_if_protected(self, path: str) -> None:
        normalized = os.path.normpath(path)
        if normalized in (_RAWOS_UNIT_FILE, _RAWOS_FRONTDOOR_SSHD_CONFIG):
            raise FileOperatorRefusalError(f"refused: {normalized} is self-protected")
        rawos_root = os.path.normpath(settings.rawos_source_root)
        if normalized == rawos_root or normalized.startswith(rawos_root + os.sep):
            raise FileOperatorRefusalError(
                f"refused: {normalized} is within rawos's own source tree ({rawos_root})"
            )


# Phase 24a — eBPF kernel perception probe script.
#
# Two probes, both observe-only:
#   - tracepoint:syscalls:sys_enter_execve — every process exec, machine-wide.
#     `comm` here is the CALLING process's name (pre-exec image); `path` is the
#     binary being exec'd, which is the useful signal.
#   - kprobe:tcp_connect — every outbound TCP connect, machine-wide. `dport` is
#     byte-swapped from network order (reliable); `daddr` may read "0.0.0.0" if
#     the kprobe fires before route resolution completes (kernel timing, not a
#     bug in this script) — comm/pid/dport remain meaningful in that case.
#
# `-f json` wraps each printf in {"type": "printf", "data": "<our JSON string>"}.
# parse_event() unwraps that envelope; lines whose payload fails to parse as
# JSON (e.g. a process/path name containing an unescaped quote) are dropped,
# never raised.
_KERNEL_PERCEPTION_SCRIPT = r'''
tracepoint:syscalls:sys_enter_execve
{
    printf("{\"event_type\":\"execve\",\"comm\":\"%s\",\"pid\":%d,\"path\":\"%s\"}\n", comm, pid, str(args->filename));
}

kprobe:tcp_connect
{
    $sk = (struct sock *)arg0;
    $daddr = ntop($sk->__sk_common.skc_daddr);
    $rport = $sk->__sk_common.skc_dport;
    $dport = (($rport & 0xff) << 8) | (($rport >> 8) & 0xff);
    printf("{\"event_type\":\"tcp_connect\",\"comm\":\"%s\",\"pid\":%d,\"daddr\":\"%s\",\"dport\":%d}\n", comm, pid, $daddr, $dport);
}
'''


class LinuxKernelObserver:
    """Linux implementation of KernelObserver — bpftrace subprocess emitting JSONL.

    Requires the `bpftrace` binary (apt package `bpftrace`) and CAP_BPF/root,
    which rawos already runs as. Read-only: the probes only printf, never
    write/deny anything.
    """

    supports_kernel_observation = True

    def probe_command(self) -> list[str]:
        return ["bpftrace", "-f", "json", "-e", _KERNEL_PERCEPTION_SCRIPT]

    def parse_event(self, line: str) -> dict | None:
        try:
            outer = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(outer, dict) or outer.get("type") != "printf":
            return None
        try:
            inner = json.loads(outer["data"])
        except (json.JSONDecodeError, TypeError, KeyError):
            return None
        if not isinstance(inner, dict):
            return None
        return inner


class LinuxKernelEnforcer:
    """Linux implementation of BPF LSM enforcement supervision (Phase 24B).

    For 24B.0 (dormant), this is a documentation stub; the actual control
    plane is rawos/kernel/bpf_lsm.py + BpfLsmSupervisor (api/app.py).

    Post-24B.1 (when holder binary + lsm= GRUB cmdline are in place), this
    class will manage the holder daemon lifecycle (spawn, healthcheck, graceful
    stop) from the arch backend layer, with the being's policy-map updates
    flowing through BpfLsmSupervisor → _SocketHolderClient → holder unix socket.

    Invariants:
      I-LSM2  — holder holds the only ref to the bpf_link (no-pin); class
                 never pins to bpffs, only tracks the holder process.
      I-LSM3  — holder is an independent systemd unit; stop() uses systemctl.
      I-LSM11 — before spawning holder, verifies engine .o + binary checksums
                 (delegated to bpf_lsm._verify_artifact).

    See rawos/kernel/bpf_lsm.py for full design.
    """

    supports_bpf_lsm_enforcement = True
    _HOLDER_UNIT = "rawos-bpf-lsm-holder.service"

    def is_bpf_lsm_available(self) -> bool:
        from rawos.kernel import bpf_lsm
        return bpf_lsm.supported()


class LinuxUnitTopologyManager:
    """Linux implementation of UnitTopologyManager via systemctl/systemd-analyze.

    NOT in Backend dataclass — mirrors LinuxKernelEnforcer pattern.
    Instantiated directly by operate_on_unit_topology() when mgr=None.

    Phase 23-full, I-UT11: ships dormant (operator_unit_topology_enabled=False).
    """

    _SYSTEMD_UNIT_DIR: str = "/etc/systemd/system"

    # --- Unit file operations ---

    def author_unit(self, unit_name: str, content: str) -> None:
        """Write content to /etc/systemd/system/<unit_name>."""
        import os
        path = os.path.join(self._SYSTEMD_UNIT_DIR, unit_name)
        with open(path, "w") as fh:
            fh.write(content)

    def delete_unit(self, unit_name: str) -> None:
        """Remove /etc/systemd/system/<unit_name> (no-op if absent)."""
        import os
        path = os.path.join(self._SYSTEMD_UNIT_DIR, unit_name)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    def read_unit(self, unit_name: str) -> "str | None":
        """Return unit file content, or None if not present."""
        import os
        path = os.path.join(self._SYSTEMD_UNIT_DIR, unit_name)
        try:
            return open(path).read()
        except FileNotFoundError:
            return None

    # --- Enable / disable ---

    def enable(self, unit_name: str) -> None:
        import subprocess
        subprocess.run(
            ["systemctl", "enable", unit_name],
            check=True, capture_output=True,
        )

    def disable(self, unit_name: str) -> None:
        import subprocess
        subprocess.run(
            ["systemctl", "disable", unit_name],
            check=True, capture_output=True,
        )

    def is_enabled(self, unit_name: str) -> bool:
        import subprocess
        result = subprocess.run(
            ["systemctl", "is-enabled", unit_name],
            capture_output=True, text=True,
        )
        return result.stdout.strip() == "enabled"

    # --- Default target ---

    def set_default(self, target: str) -> None:
        import subprocess
        subprocess.run(
            ["systemctl", "set-default", target],
            check=True, capture_output=True,
        )

    def get_default(self) -> str:
        import subprocess
        result = subprocess.run(
            ["systemctl", "get-default"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    # --- Reload ---

    def daemon_reload(self) -> None:
        import subprocess
        subprocess.run(
            ["systemctl", "daemon-reload"],
            check=True, capture_output=True,
        )

    # --- Verify / health ---

    def analyze_verify(self) -> "tuple[bool, str]":
        """Run systemd-analyze verify on all unit files in _SYSTEMD_UNIT_DIR.

        Returns (ok, output) — ok=True iff exit code 0.
        Runs on FULL config (not just new unit) as required by I-UT5.
        """
        import subprocess
        import glob
        import os
        unit_files = glob.glob(os.path.join(self._SYSTEMD_UNIT_DIR, "*"))
        if not unit_files:
            return (True, "")
        result = subprocess.run(
            ["systemd-analyze", "verify", *sorted(unit_files)],
            capture_output=True, text=True,
        )
        output = result.stdout + result.stderr
        return (result.returncode == 0, output)

    def is_active(self, unit_name: str) -> bool:
        import subprocess
        result = subprocess.run(
            ["systemctl", "is-active", unit_name],
            capture_output=True, text=True,
        )
        return result.stdout.strip() == "active"

    def is_system_running(self) -> bool:
        import subprocess
        result = subprocess.run(
            ["systemctl", "is-system-running"],
            capture_output=True, text=True,
        )
        state = result.stdout.strip()
        return state not in {"failed", "stopping"}

    def list_dependencies(self, *unit_names: str) -> str:
        import subprocess
        result = subprocess.run(
            ["systemctl", "list-dependencies", "--all", *unit_names],
            capture_output=True, text=True,
        )
        return result.stdout
