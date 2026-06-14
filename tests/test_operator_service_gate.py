"""tests/test_operator_service_gate.py — TDD for the operate_on_service gate (Phase 23a, Step 5).

Gate matrix under test:
  target not allowlisted                      → propose-only (no track-record write)
  operator_service_enabled=False              → propose-only
  target not graduated                        → propose-only
  all conditions met + validator pass         → auto-applied, track-record updated
  all conditions met + validator fail (start) → auto-applied, rolled back to inactive, record updated
  self-protection target                      → ServiceOperatorRefusalError regardless of gate

execute_approved_service_action: runs full contract + records regardless of flag/graduation.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time

import pytest

import rawos.db as db
import rawos.kernel.operator as operator_module
from rawos.kernel.operator import (
    OperatorError,
    ServiceOperateOutcome,
    ServiceOperatorRefusalError,
    execute_approved_service_action,
    operate_on_service,
)
from rawos.models import User

SERVICE_TARGET = "rawos-svcprobe.service"
RESTART_CLASS = "service_restart"
START_CLASS = "service_start"


class FakeServiceManager:
    """In-memory ServiceManager; real subprocess never called in these gate tests."""

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


def _graduate(user_id: str, operation_class: str, target: str) -> None:
    """Drive 6 verified=True updates to reach GRADUATION_THRESHOLD=3 verified_successes."""
    from rawos.kernel.track_record import GRADUATION_THRESHOLD
    now = int(time.time())
    for i in range(GRADUATION_THRESHOLD * 2):
        db.update_operator_track_record(
            user_id, operation_class, target,
            verified=True, now=now + i,
        )


class TestOperateOnServiceGate:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        db.init(os.path.join(self.tmp, "test.db"))
        self.user = db.create_user(User(
            email=f"svc-gate-{id(self)}-{os.getpid()}@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))
        self.mgr = FakeServiceManager(initially_active=True)

    # --- Gate matrix ---

    def test_propose_only_when_target_not_allowlisted(self):
        outcome = operate_on_service(
            self.user.id, SERVICE_TARGET, "restart",
            service_manager=self.mgr,
        )
        assert outcome.proposed is True
        assert outcome.auto_applied is False
        assert outcome.proposed_action == "restart"
        assert outcome.operation_result is None
        assert "allowlist" in outcome.reason

    def test_propose_only_when_service_flag_disabled(self, monkeypatch):
        db.add_managed_service_target(
            self.user.id, SERVICE_TARGET, "systemctl is-active --quiet rawos-svcprobe"
        )
        _graduate(self.user.id, RESTART_CLASS, SERVICE_TARGET)
        monkeypatch.setattr(operator_module.settings, "operator_service_enabled", False)

        outcome = operate_on_service(
            self.user.id, SERVICE_TARGET, "restart",
            service_manager=self.mgr,
        )
        assert outcome.proposed is True
        assert outcome.auto_applied is False
        assert outcome.proposed_action == "restart"
        assert "operator_service_enabled=False" in outcome.reason

    def test_propose_only_when_ungraduated(self, monkeypatch):
        db.add_managed_service_target(
            self.user.id, SERVICE_TARGET, "systemctl is-active --quiet rawos-svcprobe"
        )
        monkeypatch.setattr(operator_module.settings, "operator_service_enabled", True)

        outcome = operate_on_service(
            self.user.id, SERVICE_TARGET, "restart",
            service_manager=self.mgr,
        )
        assert outcome.proposed is True
        assert outcome.auto_applied is False
        assert "graduated" in outcome.reason

    def test_auto_applies_when_all_conditions_met(self, monkeypatch):
        db.add_managed_service_target(self.user.id, SERVICE_TARGET, "true")
        _graduate(self.user.id, RESTART_CLASS, SERVICE_TARGET)
        monkeypatch.setattr(operator_module.settings, "operator_service_enabled", True)

        outcome = operate_on_service(
            self.user.id, SERVICE_TARGET, "restart",
            service_manager=self.mgr,
        )
        assert outcome.auto_applied is True
        assert outcome.proposed is False
        assert outcome.operation_result is not None
        assert outcome.operation_result.verified is True
        assert outcome.operation_result.restored is False
        assert self.mgr.calls == ["restart"]

    def test_auto_apply_rolls_back_when_validator_fails(self, monkeypatch):
        """start: was_active=False; validator="false" → verify fails → restore stops it."""
        mgr = FakeServiceManager(initially_active=False)
        db.add_managed_service_target(self.user.id, SERVICE_TARGET, "false")
        _graduate(self.user.id, START_CLASS, SERVICE_TARGET)
        monkeypatch.setattr(operator_module.settings, "operator_service_enabled", True)

        outcome = operate_on_service(
            self.user.id, SERVICE_TARGET, "start",
            service_manager=mgr,
        )
        assert outcome.auto_applied is True
        assert outcome.operation_result.verified is False
        assert outcome.operation_result.restored is True
        assert mgr.is_active(SERVICE_TARGET) is False
        assert mgr.calls == ["start", "stop"]

    def test_refuses_self_protected_service_regardless_of_gate(self, monkeypatch):
        db.add_managed_service_target(self.user.id, "rawos.service", "true")
        _graduate(self.user.id, RESTART_CLASS, "rawos.service")
        monkeypatch.setattr(operator_module.settings, "operator_service_enabled", True)

        with pytest.raises(ServiceOperatorRefusalError):
            operate_on_service(
                self.user.id, "rawos.service", "restart",
                service_manager=self.mgr,
            )

    @pytest.mark.parametrize("protected", [
        "rawos.service", "rawos", "ssh.service", "ssh", "sshd.service", "sshd",
    ])
    def test_refuses_all_self_protected_forms(self, monkeypatch, protected):
        db.add_managed_service_target(self.user.id, protected, "true")
        _graduate(self.user.id, RESTART_CLASS, protected)
        monkeypatch.setattr(operator_module.settings, "operator_service_enabled", True)

        with pytest.raises(ServiceOperatorRefusalError):
            operate_on_service(
                self.user.id, protected, "restart",
                service_manager=self.mgr,
            )

    # --- Side-effect correctness ---

    def test_propose_only_does_not_update_track_record(self):
        operate_on_service(
            self.user.id, SERVICE_TARGET, "restart",
            service_manager=self.mgr,
        )
        record = db.get_operator_track_record(self.user.id, RESTART_CLASS, SERVICE_TARGET)
        assert record.verified_successes == 0
        assert record.graduated is False

    def test_auto_apply_updates_track_record(self, monkeypatch):
        db.add_managed_service_target(self.user.id, SERVICE_TARGET, "true")
        _graduate(self.user.id, RESTART_CLASS, SERVICE_TARGET)
        monkeypatch.setattr(operator_module.settings, "operator_service_enabled", True)

        operate_on_service(
            self.user.id, SERVICE_TARGET, "restart",
            service_manager=self.mgr,
        )
        record = db.get_operator_track_record(self.user.id, RESTART_CLASS, SERVICE_TARGET)
        assert record.graduated is True

    def test_per_action_graduation_is_independent(self, monkeypatch):
        """service_restart and service_start graduate independently."""
        db.add_managed_service_target(self.user.id, SERVICE_TARGET, "true")
        _graduate(self.user.id, RESTART_CLASS, SERVICE_TARGET)
        monkeypatch.setattr(operator_module.settings, "operator_service_enabled", True)

        start_record = db.get_operator_track_record(self.user.id, START_CLASS, SERVICE_TARGET)
        assert start_record.graduated is False


class TestExecuteApprovedServiceAction:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        db.init(os.path.join(self.tmp, "test.db"))
        self.user = db.create_user(User(
            email=f"svc-approved-{id(self)}-{os.getpid()}@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))
        self.mgr = FakeServiceManager(initially_active=True)

    def test_runs_contract_and_applies(self):
        db.add_managed_service_target(self.user.id, SERVICE_TARGET, "true")
        result = execute_approved_service_action(
            self.user.id, SERVICE_TARGET, "restart",
            service_manager=self.mgr,
        )
        assert result.applied is True
        assert result.verified is True
        assert result.restored is False
        assert self.mgr.calls == ["restart"]

    def test_records_toward_graduation(self):
        db.add_managed_service_target(self.user.id, SERVICE_TARGET, "true")
        now = int(time.time())
        # Two calls — first opens stability window, second advances verified_successes to 1.
        execute_approved_service_action(
            self.user.id, SERVICE_TARGET, "restart",
            service_manager=self.mgr, now=now,
        )
        execute_approved_service_action(
            self.user.id, SERVICE_TARGET, "restart",
            service_manager=self.mgr, now=now + 1,
        )
        record = db.get_operator_track_record(self.user.id, RESTART_CLASS, SERVICE_TARGET)
        assert record.verified_successes == 1

    def test_raises_for_unregistered_target(self):
        with pytest.raises(OperatorError):
            execute_approved_service_action(
                self.user.id, "never-added.service", "restart",
                service_manager=self.mgr,
            )

    def test_runs_without_operator_service_enabled_flag(self, monkeypatch):
        """execute_approved_service_action does not check operator_service_enabled."""
        db.add_managed_service_target(self.user.id, SERVICE_TARGET, "true")
        monkeypatch.setattr(operator_module.settings, "operator_service_enabled", False)

        result = execute_approved_service_action(
            self.user.id, SERVICE_TARGET, "restart",
            service_manager=self.mgr,
        )
        assert result.applied is True
        assert result.verified is True

    def test_self_protected_refuses_even_on_approved_path(self):
        db.add_managed_service_target(self.user.id, "rawos.service", "true")
        with pytest.raises(ServiceOperatorRefusalError):
            execute_approved_service_action(
                self.user.id, "rawos.service", "restart",
                service_manager=self.mgr,
            )
