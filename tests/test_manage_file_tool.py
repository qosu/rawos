"""tests/test_manage_file_tool.py — TDD for manage_file tool in REGISTRY (Milestone 3, §7, Step 6).

Calls execute("manage_file", params, workdir) with a billing_context set,
verifying the full round-trip through the tool registry.

All tests run inside set_billing_context to simulate the agent_loop context.
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


class TestManageFileTool:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        db.init(os.path.join(self.tmp, "test.db"))
        self.user = db.create_user(User(
            email=f"mft-{id(self)}-{os.getpid()}@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))
        self.workdir = self.tmp
        self.target = os.path.join(self.tmp, "managed.conf")
        with open(self.target, "wb") as f:
            f.write(b"original\n")

    async def _exec(self, params: dict) -> object:
        with set_billing_context(user_id=self.user.id, intent_id="test-intent", event_type="test"):
            return await execute("manage_file", params, self.workdir)

    # --- add_target ---

    async def test_add_target_registers_in_db(self):
        result = await self._exec({
            "action": "add_target",
            "target_path": self.target,
            "validator_cmd": "true",
        })
        assert result.success is True
        row = db.get_managed_file_target(self.user.id, self.target)
        assert row is not None
        assert row["validator_cmd"] == "true"

    async def test_add_target_missing_validator_fails(self):
        result = await self._exec({
            "action": "add_target",
            "target_path": self.target,
            "validator_cmd": "",
        })
        assert result.success is False

    # --- remove_target ---

    async def test_remove_target_deregisters_from_db(self):
        db.add_managed_file_target(self.user.id, self.target, "true")
        result = await self._exec({
            "action": "remove_target",
            "target_path": self.target,
        })
        assert result.success is True
        assert db.get_managed_file_target(self.user.id, self.target) is None

    # --- status ---

    async def test_status_registered_target(self):
        db.add_managed_file_target(self.user.id, self.target, "caddy validate")
        result = await self._exec({
            "action": "status",
            "target_path": self.target,
        })
        assert result.success is True
        assert "caddy validate" in result.output
        assert "graduated" in result.output

    async def test_status_unregistered_target_says_not_allowlisted(self):
        result = await self._exec({
            "action": "status",
            "target_path": "/etc/never-added.conf",
        })
        assert result.success is True
        assert "not allowlisted" in result.output

    # --- edit (propose-only when not graduated) ---

    async def test_edit_returns_propose_only_when_not_graduated(self, monkeypatch):
        db.add_managed_file_target(self.user.id, self.target, "true")
        monkeypatch.setattr(operator_module.settings, "operator_enabled", True)

        result = await self._exec({
            "action": "edit",
            "target_path": self.target,
            "new_content": "new content\n",
        })
        assert result.success is True
        assert "proposed" in result.output
        # file unchanged
        assert open(self.target, "rb").read() == b"original\n"

    async def test_edit_auto_applies_when_graduated(self, monkeypatch):
        db.add_managed_file_target(self.user.id, self.target, "true")
        # Graduate: 6 verified=True calls
        now = int(time.time())
        for _ in range(6):
            db.update_operator_track_record(
                self.user.id, "file_edit", self.target, verified=True, now=now,
            )
        monkeypatch.setattr(operator_module.settings, "operator_enabled", True)

        result = await self._exec({
            "action": "edit",
            "target_path": self.target,
            "new_content": "auto-applied content\n",
        })
        assert result.success is True
        assert "auto-applied" in result.output
        assert open(self.target, "rb").read() == b"auto-applied content\n"

    # --- approved_apply ---

    async def test_approved_apply_executes_contract(self):
        db.add_managed_file_target(self.user.id, self.target, "true")
        result = await self._exec({
            "action": "approved_apply",
            "target_path": self.target,
            "new_content": "owner approved content\n",
        })
        assert result.success is True
        assert open(self.target, "rb").read() == b"owner approved content\n"

    async def test_approved_apply_fails_for_unregistered_target(self):
        result = await self._exec({
            "action": "approved_apply",
            "target_path": "/etc/never-added.conf",
            "new_content": "content\n",
        })
        assert result.success is False
        assert "operator error" in result.output

    # --- error handling ---

    async def test_unknown_action_returns_failure(self):
        result = await self._exec({"action": "explode"})
        assert result.success is False
        assert "unknown action" in result.output

    async def test_no_billing_context_returns_failure(self):
        # Call without wrapping in set_billing_context
        result = await execute("manage_file", {"action": "status", "target_path": self.target}, self.workdir)
        assert result.success is False
        assert "no active agent context" in result.output
