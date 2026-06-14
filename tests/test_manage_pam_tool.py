"""tests/test_manage_pam_tool.py — TDD for manage_pam tool in REGISTRY (Phase 22).

Calls execute("manage_pam", params, workdir) with billing_context set,
verifying full round-trip through the tool registry.

_probe_fn and _systemd are injected via params (_test_probe_fn / _test_systemd)
to avoid real SSH and systemd-run calls.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

import pytest

import rawos.db as db
from rawos.kernel.billing_context import set_billing_context
from rawos.kernel.tools import execute
from rawos.models import User

PAM_TARGET = "rawos-guest"


class FakePamDeadman:
    def __init__(self) -> None:
        self.armed: list = []
        self.disarmed: list = []

    def arm(self, unit, delay_s, revert_cmd):
        self.armed.append((unit, delay_s, revert_cmd))

    def disarm(self, unit):
        self.disarmed.append(unit)


class TestManagePamTool:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        db.init(os.path.join(self.tmp, "test.db"))
        self.user = db.create_user(User(
            email=f"mpt-{id(self)}-{os.getpid()}@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))
        self.pam_dir = Path(self.tmp) / "pam.d"
        self.pam_dir.mkdir()
        self.backup_dir = Path(self.tmp) / "backups"
        self.backup_dir.mkdir()
        self.fake_sd = FakePamDeadman()

    async def _exec(self, params: dict) -> object:
        with set_billing_context(user_id=self.user.id, intent_id="test-intent", event_type="test"):
            return await execute("manage_pam", params, self.tmp)

    def _pam_params(self, extra: dict | None = None) -> dict:
        base = {
            "_test_pam_dir": str(self.pam_dir),
            "_test_backup_dir": str(self.backup_dir),
            "_test_systemd": self.fake_sd,
        }
        if extra:
            base.update(extra)
        return base

    # --- add_target ---

    async def test_add_target_registers_in_db(self) -> None:
        result = await self._exec({
            "action": "add_target",
            "pam_file": PAM_TARGET,
        })
        assert result.success is True
        assert db.get_managed_pam_target(self.user.id, PAM_TARGET) is not None

    async def test_add_target_missing_pam_file_fails(self) -> None:
        result = await self._exec({"action": "add_target"})
        assert result.success is False

    # --- remove_target ---

    async def test_remove_target_deregisters(self) -> None:
        db.add_managed_pam_target(self.user.id, PAM_TARGET)
        result = await self._exec({"action": "remove_target", "pam_file": PAM_TARGET})
        assert result.success is True
        assert db.get_managed_pam_target(self.user.id, PAM_TARGET) is None

    async def test_remove_target_missing_pam_file_fails(self) -> None:
        result = await self._exec({"action": "remove_target"})
        assert result.success is False

    # --- status ---

    async def test_status_registered_target(self) -> None:
        db.add_managed_pam_target(self.user.id, PAM_TARGET)
        result = await self._exec({"action": "status", "pam_file": PAM_TARGET})
        assert result.success is True
        assert PAM_TARGET in result.output
        assert "protected" in result.output

    async def test_status_unregistered_target(self) -> None:
        result = await self._exec({"action": "status", "pam_file": "never-added"})
        assert result.success is True
        assert "not allowlisted" in result.output

    async def test_status_protected_file_warns(self) -> None:
        result = await self._exec({"action": "status", "pam_file": "sshd"})
        assert result.success is True
        assert "self-protected" in result.output

    # --- approved_apply ---

    async def test_approved_apply_success(self) -> None:
        db.add_managed_pam_target(self.user.id, PAM_TARGET)
        (self.pam_dir / PAM_TARGET).write_text("original")
        result = await self._exec(self._pam_params({
            "action": "approved_apply",
            "pam_file": PAM_TARGET,
            "new_content": "new pam config",
            "_test_probe_fn": True,  # probe passes
        }))
        assert result.success is True
        assert "armed" in result.output.lower() or "snapshot" in result.output.lower()
        assert (self.pam_dir / PAM_TARGET).read_text() == "new pam config"
        assert len(self.fake_sd.armed) == 1

    async def test_approved_apply_probe_fail_restores(self) -> None:
        db.add_managed_pam_target(self.user.id, PAM_TARGET)
        (self.pam_dir / PAM_TARGET).write_text("original")
        result = await self._exec(self._pam_params({
            "action": "approved_apply",
            "pam_file": PAM_TARGET,
            "new_content": "bad pam config",
            "_test_probe_fn": False,
        }))
        assert result.success is False
        assert "probe" in result.output.lower() or "install" in result.output.lower()
        assert (self.pam_dir / PAM_TARGET).read_text() == "original"

    async def test_approved_apply_protected_file_refuses(self) -> None:
        db.add_managed_pam_target(self.user.id, "sshd")
        result = await self._exec(self._pam_params({
            "action": "approved_apply",
            "pam_file": "sshd",
            "new_content": "bad",
            "_test_probe_fn": True,
        }))
        assert result.success is False
        assert "self-protect" in result.output.lower() or "refused" in result.output.lower()

    async def test_approved_apply_not_allowlisted_fails(self) -> None:
        result = await self._exec(self._pam_params({
            "action": "approved_apply",
            "pam_file": "rawos-tenant",
            "new_content": "pam config",
            "_test_probe_fn": True,
        }))
        assert result.success is False
        assert "not in" in result.output.lower() or "allowlist" in result.output.lower()

    # --- commit ---

    async def test_commit_disarms_deadman(self) -> None:
        result = await self._exec(self._pam_params({"action": "commit"}))
        assert result.success is True
        assert self.fake_sd.disarmed == ["rawos-pam-revert"]

    # --- error handling ---

    async def test_unknown_action_fails(self) -> None:
        result = await self._exec({"action": "invalid-action"})
        assert result.success is False
        assert "unknown action" in result.output

    async def test_no_billing_context_fails(self) -> None:
        result = await execute("manage_pam", {"action": "status", "pam_file": PAM_TARGET}, self.tmp)
        assert result.success is False
        assert "no active agent context" in result.output
