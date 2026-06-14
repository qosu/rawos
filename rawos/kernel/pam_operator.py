"""rawos/kernel/pam_operator.py — Phase 22 PAM operator (R3-adjacent).

SAFETY INVARIANTS (never touch these without docs/phase22_pam_invariants.md review):

I1  _SELF_PROTECTED_PAM_FILES: refuse-at-construction, cannot be bypassed.
I2  Break-glass account must exist before any PAM write is activated.
I3  Deadman armed BEFORE apply — PamFileEdit.capture() returns before arm().
I4  Revert cmd runs as systemd root context, not via SSH PAM.
I5  verify() uses dedicated on-box probe key only — never operator key.
I6  Probe is autonomous — no operator lifeline session required.
I7  No operate_on_pam() — zero autonomous PAM write path. Ever.
I8  operator_pam_enabled=False by default (dormant).
I9  Snapshot stored at PAM_BACKUP_DIR/<uuid> on disk, NOT in rawos.db.
"""
from __future__ import annotations

import subprocess
import uuid as _uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import rawos.db as db

PAM_DIR = Path("/etc/pam.d")
PAM_BACKUP_DIR = Path("/root/.rawos-pam-backups")
PROBE_KEY_PATH = Path("/root/.rawos-pam-backups/probe_key")
PROBE_HOST = "root@127.0.0.1"
PROBE_TIMEOUT_S = 10
PAM_DEADMAN_UNIT = "rawos-pam-revert"
PAM_DEADMAN_DELAY_S = 300

_SELF_PROTECTED_PAM_FILES = frozenset({
    "sshd",
    "common-auth",
    "common-account",
    "common-password",
    "common-session",
    "common-session-noninteractive",
    "su",
    "sudo",
    "sudo-i",
    "login",
    "runuser",
    "runuser-l",
    "chfn",
    "chpasswd",
    "chsh",
    "passwd",
    "newusers",
    "other",
    "atd",
    "cron",
    "vmtoolsd",
})


class PamRefusalError(Exception):
    """Raised at PamFileEdit construction for any self-protected pam.d target.

    Cannot be caught and retried with different flags — the protection is
    structural, not conditional.
    """


class PamInstallError(RuntimeError):
    """Raised by install_pam_edit_with_deadman when apply+verify fails.

    Previous config is guaranteed restored and deadman disarmed before raise.
    """


@dataclass(frozen=True)
class PamSnapshot:
    pam_file: str
    was_absent: bool
    prior_content: bytes
    snapshot_id: str    # UUID4 string
    backup_path: str    # absolute path to on-disk backup (I9)


class _PamDeadmanSystemd:
    """Real systemd-run deadman for production use."""

    def arm(self, unit: str, delay_s: int, revert_cmd: str) -> None:
        subprocess.run(
            [
                "systemd-run",
                f"--on-active={delay_s}",
                f"--unit={unit}",
                "--",
                *revert_cmd.split(),
            ],
            check=True,
            capture_output=True,
            timeout=10.0,
        )

    def disarm(self, unit: str) -> None:
        subprocess.run(
            ["systemctl", "stop", f"{unit}.timer"],
            capture_output=True,
            timeout=5.0,
        )


def _run_ssh_probe(
    probe_key: Path,
    host: str,
    timeout_s: int,
) -> bool:
    try:
        result = subprocess.run(
            [
                "ssh",
                "-i", str(probe_key),
                "-o", "ControlMaster=no",
                "-o", "ControlPath=none",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=no",
                "-o", f"ConnectTimeout={timeout_s}",
                host,
                "true",
            ],
            capture_output=True,
            timeout=timeout_s + 5,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


class PamFileEdit:
    """ReversibleOperation for a non-root-critical pam.d file.

    Raises PamRefusalError at construction if pam_file is in
    _SELF_PROTECTED_PAM_FILES — this check cannot be bypassed by any flag.

    Snapshot is written to PAM_BACKUP_DIR/<uuid> at capture() time (I9).
    restore() reads from disk, not from in-memory prior_content, so it
    works even after process restart.
    """

    def __init__(
        self,
        pam_file: str,
        new_content: bytes,
        *,
        _probe_fn: "Callable[[], bool] | None" = None,
        _pam_dir: "Path | None" = None,
        _backup_dir: "Path | None" = None,
        _probe_key: "Path | None" = None,
    ) -> None:
        if pam_file in _SELF_PROTECTED_PAM_FILES:
            raise PamRefusalError(
                f"refused: {pam_file!r} is in _SELF_PROTECTED_PAM_FILES "
                "(lockout-safety floor, cannot be bypassed)"
            )
        self.pam_file = pam_file
        self._new_content = new_content
        self._probe_fn = _probe_fn
        self._pam_dir = _pam_dir if _pam_dir is not None else PAM_DIR
        self._backup_dir = _backup_dir if _backup_dir is not None else PAM_BACKUP_DIR
        self._probe_key = _probe_key if _probe_key is not None else PROBE_KEY_PATH

    def capture(self) -> PamSnapshot:
        target = self._pam_dir / self.pam_file
        if target.exists():
            prior = target.read_bytes()
            was_absent = False
        else:
            prior = b""
            was_absent = True

        snapshot_id = str(_uuid.uuid4())
        backup_path = self._backup_dir / snapshot_id
        backup_path.write_bytes(prior)

        return PamSnapshot(
            pam_file=self.pam_file,
            was_absent=was_absent,
            prior_content=prior,
            snapshot_id=snapshot_id,
            backup_path=str(backup_path),
        )

    def apply(self) -> None:
        (self._pam_dir / self.pam_file).write_bytes(self._new_content)

    def verify(self) -> bool:
        if self._probe_fn is not None:
            return self._probe_fn()
        return _run_ssh_probe(self._probe_key, PROBE_HOST, PROBE_TIMEOUT_S)

    def restore(self, snap: PamSnapshot) -> None:
        target = self._pam_dir / snap.pam_file
        backup = Path(snap.backup_path)
        prior = backup.read_bytes()
        if snap.was_absent:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(prior)


def install_pam_edit_with_deadman(
    pam_file: str,
    new_content: bytes,
    revert_after_s: int = PAM_DEADMAN_DELAY_S,
    *,
    _systemd: object = None,
    _probe_fn: "Callable[[], bool] | None" = None,
    _pam_dir: "Path | None" = None,
    _backup_dir: "Path | None" = None,
    _probe_key: "Path | None" = None,
) -> str:
    """Install a PAM edit with an automatic safety revert (I3, I4).

    Order of operations (must not deviate — see I3):
        1. capture  — snapshot current pam.d state to disk
        2. arm      — schedule rawos-pam-revert timer (systemd root context)
        3. apply    — write new pam.d content
        4. verify   — live-auth probe via dedicated probe key (I5, I6)
           * fail or exception: disarm + restore + raise PamInstallError
    Returns snapshot_id (UUID string) — system is now ARMED.
    Caller must call commit_pam_edit() after out-of-band verification.

    PamRefusalError for protected targets propagates before arm (I1).
    """
    sd = _systemd if _systemd is not None else _PamDeadmanSystemd()
    op = PamFileEdit(
        pam_file, new_content,
        _probe_fn=_probe_fn,
        _pam_dir=_pam_dir,
        _backup_dir=_backup_dir,
        _probe_key=_probe_key,
    )
    snap = op.capture()
    revert_cmd = f"/usr/local/bin/rawos-pam-restore {snap.snapshot_id} {pam_file}"
    sd.arm(PAM_DEADMAN_UNIT, revert_after_s, revert_cmd)

    try:
        op.apply()
        if not op.verify():
            sd.disarm(PAM_DEADMAN_UNIT)
            op.restore(snap)
            raise PamInstallError(
                f"live-auth probe failed after applying {pam_file!r}. "
                "Previous config restored. Deadman disarmed."
            )
    except PamInstallError:
        raise
    except Exception as exc:
        sd.disarm(PAM_DEADMAN_UNIT)
        op.restore(snap)
        raise PamInstallError(
            f"Unexpected error during PAM edit of {pam_file!r}; "
            f"previous config restored. Cause: {exc}"
        ) from exc

    return snap.snapshot_id


def commit_pam_edit(*, _systemd: object = None) -> None:
    """Disarm the rawos-pam-revert deadman timer after out-of-band verification."""
    sd = _systemd if _systemd is not None else _PamDeadmanSystemd()
    sd.disarm(PAM_DEADMAN_UNIT)


def execute_approved_pam_edit(
    user_id: str,
    pam_file: str,
    new_content: bytes,
    revert_after_s: int = PAM_DEADMAN_DELAY_S,
    *,
    _systemd: object = None,
    _probe_fn: "Callable[[], bool] | None" = None,
    _pam_dir: "Path | None" = None,
    _backup_dir: "Path | None" = None,
    _probe_key: "Path | None" = None,
) -> str:
    """Execute an owner-approved PAM edit after allowlist check.

    Requires pam_file to be in managed_pam_targets for user_id.
    Does not check operator_pam_enabled (I7 — no autonomous path).
    PamRefusalError for protected targets propagates unconditionally.
    Returns snapshot_id (ARMED — caller must call commit_pam_edit()).
    """
    from rawos.kernel.operator import OperatorError
    managed = db.get_managed_pam_target(user_id, pam_file)
    if managed is None:
        raise OperatorError(
            f"execute_approved_pam_edit: {pam_file!r} not in managed_pam_targets allowlist"
        )
    return install_pam_edit_with_deadman(
        pam_file, new_content, revert_after_s,
        _systemd=_systemd,
        _probe_fn=_probe_fn,
        _pam_dir=_pam_dir,
        _backup_dir=_backup_dir,
        _probe_key=_probe_key,
    )
