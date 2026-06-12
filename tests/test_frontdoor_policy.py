"""
Policy layer tests for rawos.kernel.frontdoor.

TDD: these tests are written FIRST. They fail until the production code
is in place.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# decide_entry — pure function, zero side effects
# ---------------------------------------------------------------------------

class TestDecideEntry:
    def _decide(self, ssh_original_command: str, rawos_healthy: bool, has_token: bool):
        from rawos.kernel.frontdoor import decide_entry, FrontDoorPolicy
        policy = FrontDoorPolicy(
            fail_open=True,
            health_url="http://127.0.0.1:8002/health",
            audit_path="/dev/null",
        )
        ctx = {
            "ssh_original_command": ssh_original_command,
            "rawos_healthy": rawos_healthy,
            "has_token": has_token,
        }
        return decide_entry(ctx, policy)

    def test_interactive_healthy_token_launches_chat(self):
        from rawos.kernel.frontdoor import EntryActionKind
        action = self._decide("", True, True)
        assert action.kind == EntryActionKind.LAUNCH_CHAT

    def test_command_present_is_passthrough_regardless_of_health(self):
        from rawos.kernel.frontdoor import EntryActionKind
        action = self._decide("ls -la", True, True)
        assert action.kind == EntryActionKind.PASSTHROUGH
        assert action.command == "ls -la"

    def test_bash_command_is_passthrough_escape_hatch(self):
        from rawos.kernel.frontdoor import EntryActionKind
        action = self._decide("bash", True, True)
        assert action.kind == EntryActionKind.PASSTHROUGH
        assert action.command == "bash"

    def test_interactive_unhealthy_fails_open_to_shell(self):
        from rawos.kernel.frontdoor import EntryActionKind
        action = self._decide("", False, True)
        assert action.kind == EntryActionKind.FAIL_OPEN_SHELL

    def test_interactive_no_token_fails_open_to_shell(self):
        from rawos.kernel.frontdoor import EntryActionKind
        action = self._decide("", True, False)
        assert action.kind == EntryActionKind.FAIL_OPEN_SHELL

    def test_interactive_unhealthy_no_token_fails_open(self):
        from rawos.kernel.frontdoor import EntryActionKind
        action = self._decide("", False, False)
        assert action.kind == EntryActionKind.FAIL_OPEN_SHELL

    def test_command_present_unhealthy_still_passthrough(self):
        """Any explicit command passes through — scp/rsync/git must never be gated."""
        from rawos.kernel.frontdoor import EntryActionKind
        action = self._decide("rsync --server .", False, False)
        assert action.kind == EntryActionKind.PASSTHROUGH


# ---------------------------------------------------------------------------
# Audit logging — each branch writes a structured JSON line
# ---------------------------------------------------------------------------

class TestAuditLogging:
    def _decide_with_audit(self, ssh_original_command: str, rawos_healthy: bool, has_token: bool):
        from rawos.kernel.frontdoor import decide_entry, FrontDoorPolicy
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            audit_path = f.name
        policy = FrontDoorPolicy(
            fail_open=True,
            health_url="http://127.0.0.1:8002/health",
            audit_path=audit_path,
        )
        ctx = {
            "ssh_original_command": ssh_original_command,
            "rawos_healthy": rawos_healthy,
            "has_token": has_token,
        }
        decide_entry(ctx, policy)
        lines = Path(audit_path).read_text().strip().splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def test_launch_chat_writes_audit_line(self):
        events = self._decide_with_audit("", True, True)
        assert len(events) == 1
        assert events[0]["action"] == "LAUNCH_CHAT"

    def test_passthrough_writes_audit_line_with_command(self):
        events = self._decide_with_audit("git-upload-pack /repo", True, True)
        assert len(events) == 1
        assert events[0]["action"] == "PASSTHROUGH"
        assert events[0]["command"] == "git-upload-pack /repo"

    def test_fail_open_writes_audit_line_with_reason(self):
        events = self._decide_with_audit("", False, False)
        assert len(events) == 1
        assert events[0]["action"] == "FAIL_OPEN_SHELL"
        assert "reason" in events[0]


# ---------------------------------------------------------------------------
# install_with_deadman — ordering + abort-on-bad-validate contract
# ---------------------------------------------------------------------------

class _RecordingArch:
    """Fake FrontDoor arch: records calls in order, configurable validate result."""

    def __init__(self, validate_result: bool = True):
        self.calls: list[str] = []
        self._validate_result = validate_result
        self._snap_counter = 0

    def snapshot(self) -> str:
        self._snap_counter += 1
        snap = f"snap-{self._snap_counter}"
        self.calls.append(f"snapshot->{snap}")
        return snap

    def restore(self, snapshot: str) -> None:
        self.calls.append(f"restore({snapshot})")

    def install(self, entry_command: str) -> None:
        self.calls.append(f"install({entry_command})")

    def validate(self) -> bool:
        self.calls.append("validate")
        return self._validate_result

    def reload(self) -> None:
        self.calls.append("reload")

    def uninstall(self) -> None:
        self.calls.append("uninstall")

    def state(self):
        from rawos.kernel.arch.base import FrontDoorState
        return FrontDoorState(installed=False, entry_command=None, config_path=None)


class _RecordingSystemd:
    """Fake systemd helper: records arm/disarm calls."""

    def __init__(self):
        self.calls: list[str] = []

    def arm(self, unit: str, delay_s: int, revert_cmd: str) -> None:
        self.calls.append(f"arm({unit},{delay_s})")

    def disarm(self, unit: str) -> None:
        self.calls.append(f"disarm({unit})")


class TestInstallWithDeadman:
    def test_happy_path_order(self):
        """snapshot → arm → install → validate → reload — in that exact order."""
        from rawos.kernel.frontdoor import install_with_deadman
        arch = _RecordingArch(validate_result=True)
        sd = _RecordingSystemd()
        install_with_deadman(
            arch, "rawos frontdoor enter", revert_after_s=300, _systemd=sd
        )
        # snapshot must come first
        assert arch.calls[0] == "snapshot->snap-1"
        # install before validate
        install_idx = arch.calls.index("install(rawos frontdoor enter)")
        validate_idx = arch.calls.index("validate")
        reload_idx = arch.calls.index("reload")
        assert install_idx < validate_idx < reload_idx
        # arm must have happened
        assert sd.calls[0].startswith("arm(")

    def test_validate_false_aborts_before_reload_and_restores(self):
        """validate()==False → restore called, reload NOT called."""
        from rawos.kernel.frontdoor import install_with_deadman, FrontDoorInstallError
        arch = _RecordingArch(validate_result=False)
        sd = _RecordingSystemd()
        with pytest.raises(FrontDoorInstallError):
            install_with_deadman(
                arch, "rawos frontdoor enter", revert_after_s=300, _systemd=sd
            )
        assert "reload" not in arch.calls
        restore_calls = [c for c in arch.calls if c.startswith("restore(")]
        assert len(restore_calls) == 1
        assert "snap-1" in restore_calls[0]
        # disarm must be called (timer arming is rolled back)
        assert any("disarm" in c for c in sd.calls)

    def test_commit_disarms_timer(self):
        from rawos.kernel.frontdoor import commit
        sd = _RecordingSystemd()
        commit(_systemd=sd)
        assert any("disarm" in c for c in sd.calls)
