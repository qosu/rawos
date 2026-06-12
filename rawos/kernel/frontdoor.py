"""kernel/frontdoor — portable front-door policy for rawos.

The front-door is the mechanism that makes an interactive login land in the
AI, not in a raw shell.  This module owns *what to do* when a login arrives
(the policy); the arch backend (e.g. linux.LinuxFrontDoor) owns *how* the
host OS routes logins here.

Public surface
--------------
FrontDoorPolicy       — configuration: fail_open, health_url, audit_path
EntryActionKind       — LAUNCH_CHAT | PASSTHROUGH | FAIL_OPEN_SHELL
EntryAction           — (kind, optional command)
decide_entry()        — pure function: ctx → EntryAction (zero side effects
                        except writing one JSON audit line)
FrontDoorInstallError — raised by install_with_deadman on failure
install_with_deadman()— safe installer: snapshot → arm → install → validate
                        → reload; aborts + restores if validate fails
commit()              — disarm the auto-revert timer after a verified install
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

_DEADMAN_UNIT = "rawos-frontdoor-revert"


# ---------------------------------------------------------------------------
# Policy configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FrontDoorPolicy:
    """Portable policy — same across all arch backends."""

    fail_open: bool
    health_url: str
    audit_path: str


# ---------------------------------------------------------------------------
# Decision types
# ---------------------------------------------------------------------------

class EntryActionKind(Enum):
    LAUNCH_CHAT = "LAUNCH_CHAT"
    PASSTHROUGH = "PASSTHROUGH"
    FAIL_OPEN_SHELL = "FAIL_OPEN_SHELL"


@dataclass(frozen=True)
class EntryAction:
    kind: EntryActionKind
    command: str | None = None  # populated for PASSTHROUGH only


# ---------------------------------------------------------------------------
# Core decision function — pure (only side effect: audit log append)
# ---------------------------------------------------------------------------

def decide_entry(ctx: dict[str, Any], policy: FrontDoorPolicy) -> EntryAction:
    """Decide the entry action for an incoming login.

    ctx keys:
        ssh_original_command: str  — value of $SSH_ORIGINAL_COMMAND ("" if interactive)
        rawos_healthy: bool        — True if the rawos API responded to /health
        has_token: bool            — True if a valid auth token exists on disk

    Returns an EntryAction.  Always appends one JSON line to policy.audit_path.

    This function has no other side effects.  exec(), subprocess launch, and
    UI output are the caller's responsibility.
    """
    cmd = ctx.get("ssh_original_command", "")
    healthy = bool(ctx.get("rawos_healthy", False))
    has_token = bool(ctx.get("has_token", False))

    if cmd:
        action = EntryAction(kind=EntryActionKind.PASSTHROUGH, command=cmd)
        _audit(policy.audit_path, {"action": "PASSTHROUGH", "command": cmd})
        return action

    if healthy and has_token:
        action = EntryAction(kind=EntryActionKind.LAUNCH_CHAT)
        _audit(policy.audit_path, {"action": "LAUNCH_CHAT"})
        return action

    reason = "unhealthy" if not healthy else "no_token"
    action = EntryAction(kind=EntryActionKind.FAIL_OPEN_SHELL)
    _audit(policy.audit_path, {"action": "FAIL_OPEN_SHELL", "reason": reason})
    return action


def _audit(audit_path: str, record: dict[str, Any]) -> None:
    """Append one JSON line to audit_path.  Silent on failure (fail-open)."""
    try:
        with open(audit_path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Dead-man's-switch systemd helper (injectable for tests)
# ---------------------------------------------------------------------------

class _DeadmanSystemd:
    """Thin wrapper around systemd-run / systemctl for the revert timer.

    Injected as _systemd parameter in install_with_deadman / commit so tests
    can substitute a fake without subprocess calls.
    """

    def arm(self, unit: str, delay_s: int, revert_cmd: str) -> None:
        """Schedule `revert_cmd` to run in `delay_s` seconds via systemd-run."""
        subprocess.run(
            [
                "systemd-run",
                "--on-active", str(delay_s),
                f"--unit={unit}",
                "--",
                *revert_cmd.split(),
            ],
            check=True,
            capture_output=True,
            timeout=10.0,
        )

    def disarm(self, unit: str) -> None:
        """Stop the revert timer unit if it is running."""
        subprocess.run(
            ["systemctl", "stop", f"{unit}.timer"],
            capture_output=True,
            timeout=5.0,
        )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class FrontDoorInstallError(RuntimeError):
    """Raised when install_with_deadman cannot complete safely."""


# ---------------------------------------------------------------------------
# Safe installer
# ---------------------------------------------------------------------------

def install_with_deadman(
    arch: Any,
    entry_command: str,
    revert_after_s: int = 300,
    *,
    _systemd: Any = None,
) -> None:
    """Install the front-door with an automatic safety revert.

    Order of operations (must not deviate):
        1. snapshot — capture current config state
        2. arm      — schedule auto-revert in revert_after_s seconds
        3. install  — write new config
        4. validate — syntactic check (e.g. sshd -t)
           * on failure: disarm + restore + raise FrontDoorInstallError
        5. reload   — apply the change

    After this returns the front-door is LIVE but ARMED.  The caller must open
    a new session, verify that the front-door and escape hatch both work, then
    call commit() to disarm.  If anything goes wrong the timer fires and
    restores the previous config automatically.
    """
    sd = _systemd if _systemd is not None else _DeadmanSystemd()
    snap = arch.snapshot()
    revert_cmd = f"rawos frontdoor _revert {snap}"
    sd.arm(_DEADMAN_UNIT, revert_after_s, revert_cmd)

    try:
        arch.install(entry_command)
        if not arch.validate():
            sd.disarm(_DEADMAN_UNIT)
            arch.restore(snap)
            raise FrontDoorInstallError(
                "sshd config failed validation (sshd -t returned non-zero). "
                "Front-door was NOT activated. Previous config restored."
            )
        arch.reload()
    except FrontDoorInstallError:
        raise
    except Exception as exc:
        sd.disarm(_DEADMAN_UNIT)
        arch.restore(snap)
        raise FrontDoorInstallError(
            f"Unexpected error during install; previous config restored. Cause: {exc}"
        ) from exc


def commit(*, _systemd: Any = None) -> None:
    """Disarm the auto-revert timer after a successful front-door verification.

    Call this only after a *new* SSH session has proven:
    - interactive ssh lands in the AI chat, and
    - ssh -t host bash drops to a raw shell (escape hatch works).
    """
    sd = _systemd if _systemd is not None else _DeadmanSystemd()
    sd.disarm(_DEADMAN_UNIT)
