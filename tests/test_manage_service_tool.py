"""tests/test_manage_service_tool.py — TDD for manage_service tool in REGISTRY (Phase 23a, Step 6).

Calls execute("manage_service", params, workdir) with a billing_context set,
verifying the full round-trip through the tool registry.

FakeServiceManager is injected via monkeypatch on get_arch().service_manager to avoid
real systemctl calls; operate_on_service / execute_approved_service_action resolve
it via get_arch() at call time.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time

import pytest

import rawos.db as db
import rawos.kernel.operator as operator_module
from rawos.kernel.billing_context import set_billing_context
from rawos.kernel.tools import execute
from rawos.models import User

SERVICE_TARGET = "rawos-svcprobe.service"
RESTART_CLASS = "service_restart"


class FakeServiceManager:
    supports_reversible_apply = True
    supports_service_ops = True

    def __init__(self, *, initially_active: bool = True) -> None:
        self._active = initially_active
        self.calls: list[str] = []

    def is_active(self, name: str) -> bool:
        return self._active

    def restart(self, name: str) -> bool:
        self.calls.append("restart")
        self._active = True
        return True

    def start(self, name: str) -> bool:
        self.calls.append("start")
        self._active = True
        return True

    def stop(self, name: str) -> bool:
        self.calls.append("stop")
        self._active = False
        return True

    def list_failed(self) -> list[str]:
        return []


class FakeArch:
    def __init__(self, mgr: FakeServiceManager) -> None:
        self.service_manager = mgr


def _graduate(user_id: str, operation_class: str, target: str) -> None:
    from rawos.kernel.track_record import GRADUATION_THRESHOLD
    now = int(time.time())
    for i in range(GRADUATION_THRESHOLD * 2):
        db.update_operator_track_record(
            user_id, operation_class, target, verified=True, now=now + i,
        )


class TestManageServiceTool:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        db.init(os.path.join(self.tmp, "test.db"))
        self.user = db.create_user(User(
            email=f"mst-{id(self)}-{os.getpid()}@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))
        self.workdir = self.tmp
        self.fake_mgr = FakeServiceManager(initially_active=True)

    async def _exec(self, params: dict) -> object:
        with set_billing_context(user_id=self.user.id, intent_id="test-intent", event_type="test"):
            return await execute("manage_service", params, self.workdir)

    def _patch_arch(self, monkeypatch) -> None:
        import rawos.kernel.arch as _arch_mod
        monkeypatch.setattr(_arch_mod, "get_arch", lambda: FakeArch(self.fake_mgr))

    # --- add_target ---

    async def test_add_target_registers_in_db(self):
        result = await self._exec({
            "action": "add_target",
            "service_name": SERVICE_TARGET,
            "validator_cmd": "systemctl is-active --quiet rawos-svcprobe",
        })
        assert result.success is True
        row = db.get_managed_service_target(self.user.id, SERVICE_TARGET)
        assert row is not None
        assert "rawos-svcprobe" in row["validator_cmd"]

    async def test_add_target_missing_validator_fails(self):
        result = await self._exec({
            "action": "add_target",
            "service_name": SERVICE_TARGET,
            "validator_cmd": "",
        })
        assert result.success is False

    # --- remove_target ---

    async def test_remove_target_deregisters_from_db(self):
        db.add_managed_service_target(self.user.id, SERVICE_TARGET, "true")
        result = await self._exec({
            "action": "remove_target",
            "service_name": SERVICE_TARGET,
        })
        assert result.success is True
        assert db.get_managed_service_target(self.user.id, SERVICE_TARGET) is None

    # --- status ---

    async def test_status_registered_target_shows_graduation(self):
        db.add_managed_service_target(self.user.id, SERVICE_TARGET, "health-check")
        result = await self._exec({
            "action": "status",
            "service_name": SERVICE_TARGET,
        })
        assert result.success is True
        assert "health-check" in result.output
        assert "graduated" in result.output
        assert "restart" in result.output

    async def test_status_unregistered_target_says_not_allowlisted(self):
        result = await self._exec({
            "action": "status",
            "service_name": "never-added.service",
        })
        assert result.success is True
        assert "not allowlisted" in result.output

    # --- action (propose-only when not graduated) ---

    async def test_action_propose_only_when_not_graduated(self, monkeypatch):
        db.add_managed_service_target(self.user.id, SERVICE_TARGET, "true")
        monkeypatch.setattr(operator_module.settings, "operator_service_enabled", True)
        self._patch_arch(monkeypatch)

        result = await self._exec({
            "action": "action",
            "service_name": SERVICE_TARGET,
            "svc_action": "restart",
        })
        assert result.success is True
        assert "proposed" in result.output
        assert self.fake_mgr.calls == []

    async def test_action_auto_applies_when_graduated(self, monkeypatch):
        db.add_managed_service_target(self.user.id, SERVICE_TARGET, "true")
        _graduate(self.user.id, RESTART_CLASS, SERVICE_TARGET)
        monkeypatch.setattr(operator_module.settings, "operator_service_enabled", True)
        self._patch_arch(monkeypatch)

        result = await self._exec({
            "action": "action",
            "service_name": SERVICE_TARGET,
            "svc_action": "restart",
        })
        assert result.success is True
        assert "auto-applied" in result.output
        assert self.fake_mgr.calls == ["restart"]

    async def test_action_refuses_self_protected_service(self, monkeypatch):
        db.add_managed_service_target(self.user.id, "rawos.service", "true")
        _graduate(self.user.id, RESTART_CLASS, "rawos.service")
        monkeypatch.setattr(operator_module.settings, "operator_service_enabled", True)
        self._patch_arch(monkeypatch)

        result = await self._exec({
            "action": "action",
            "service_name": "rawos.service",
            "svc_action": "restart",
        })
        assert result.success is False
        assert "self-protection" in result.output

    # --- approved_apply ---

    async def test_approved_apply_executes_contract(self, monkeypatch):
        db.add_managed_service_target(self.user.id, SERVICE_TARGET, "true")
        self._patch_arch(monkeypatch)

        result = await self._exec({
            "action": "approved_apply",
            "service_name": SERVICE_TARGET,
            "svc_action": "restart",
        })
        assert result.success is True
        assert self.fake_mgr.calls == ["restart"]

    async def test_approved_apply_fails_for_unregistered_target(self, monkeypatch):
        self._patch_arch(monkeypatch)
        result = await self._exec({
            "action": "approved_apply",
            "service_name": "never-added.service",
            "svc_action": "restart",
        })
        assert result.success is False
        assert "operator error" in result.output

    async def test_approved_apply_refuses_self_protected(self, monkeypatch):
        db.add_managed_service_target(self.user.id, "ssh.service", "true")
        self._patch_arch(monkeypatch)

        result = await self._exec({
            "action": "approved_apply",
            "service_name": "ssh.service",
            "svc_action": "stop",
        })
        assert result.success is False
        assert "self-protection" in result.output

    async def test_approved_apply_does_not_check_flag(self, monkeypatch):
        """execute_approved_service_action bypasses operator_service_enabled."""
        db.add_managed_service_target(self.user.id, SERVICE_TARGET, "true")
        monkeypatch.setattr(operator_module.settings, "operator_service_enabled", False)
        self._patch_arch(monkeypatch)

        result = await self._exec({
            "action": "approved_apply",
            "service_name": SERVICE_TARGET,
            "svc_action": "restart",
        })
        assert result.success is True

    # --- error handling ---

    async def test_unknown_action_returns_failure(self):
        result = await self._exec({"action": "explode"})
        assert result.success is False
        assert "unknown action" in result.output

    async def test_no_billing_context_returns_failure(self):
        result = await execute(
            "manage_service", {"action": "status", "service_name": SERVICE_TARGET}, self.workdir
        )
        assert result.success is False
        assert "no active agent context" in result.output
