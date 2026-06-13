"""tests/test_operator_gate.py — TDD for the operate_on_file gate (Milestone 3, §7, Step 5).

Gate matrix under test:
  target not allowlisted          → propose-only (no track-record write)
  operator_enabled=False          → propose-only
  target not graduated            → propose-only
  all conditions met + valid pass → auto-applied, file changed, track-record updated
  all conditions met + valid fail → auto-applied, file restored, track-record updated
  self-protection target          → FileOperatorRefusalError regardless of gate

execute_approved_file_edit: runs full contract + records regardless of operator_enabled/graduation.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time

import pytest

import rawos.db as db
import rawos.kernel.operator as operator_module
from rawos.kernel.arch.base import FileOperatorRefusalError
from rawos.kernel.arch.linux import LinuxFileOperator
from rawos.kernel.operator import (
    OperateOutcome,
    OperatorError,
    execute_approved_file_edit,
    operate_on_file,
)
from rawos.models import User

OPERATOR_CLASS = "file_edit"


class TestOperateOnFileGate:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        db.init(os.path.join(self.tmp, "test.db"))
        self.user = db.create_user(User(
            email=f"gate-{id(self)}-{os.getpid()}@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))
        self.operator = LinuxFileOperator()
        self.target = os.path.join(self.tmp, "managed.conf")
        with open(self.target, "wb") as f:
            f.write(b"original\n")
        self.new_content = b"updated\n"

    def _graduate(self, target_path: str) -> None:
        """Drive 6 verified=True updates to reach GRADUATION_THRESHOLD=3 verified_successes."""
        from rawos.kernel.track_record import GRADUATION_THRESHOLD
        now = int(time.time())
        for _ in range(GRADUATION_THRESHOLD * 2):
            db.update_operator_track_record(
                self.user.id, OPERATOR_CLASS, target_path,
                verified=True, now=now,
            )

    # --- Gate matrix ---

    def test_propose_only_when_target_not_allowlisted(self):
        outcome = operate_on_file(
            self.user.id, self.target, self.new_content,
            file_operator=self.operator,
        )
        assert outcome.proposed is True
        assert outcome.auto_applied is False
        assert outcome.proposed_content == self.new_content
        assert outcome.operation_result is None

    def test_propose_only_when_operator_disabled(self, monkeypatch):
        db.add_managed_file_target(self.user.id, self.target, "true")
        self._graduate(self.target)
        monkeypatch.setattr(operator_module.settings, "operator_enabled", False)

        outcome = operate_on_file(
            self.user.id, self.target, self.new_content,
            file_operator=self.operator,
        )
        assert outcome.proposed is True
        assert outcome.auto_applied is False
        assert outcome.proposed_content == self.new_content

    def test_propose_only_when_ungraduated(self, monkeypatch):
        db.add_managed_file_target(self.user.id, self.target, "true")
        monkeypatch.setattr(operator_module.settings, "operator_enabled", True)

        outcome = operate_on_file(
            self.user.id, self.target, self.new_content,
            file_operator=self.operator,
        )
        assert outcome.proposed is True
        assert outcome.auto_applied is False

    def test_auto_applies_when_all_conditions_met(self, monkeypatch):
        db.add_managed_file_target(self.user.id, self.target, "true")
        self._graduate(self.target)
        monkeypatch.setattr(operator_module.settings, "operator_enabled", True)

        outcome = operate_on_file(
            self.user.id, self.target, self.new_content,
            file_operator=self.operator,
        )
        assert outcome.auto_applied is True
        assert outcome.proposed is False
        assert outcome.operation_result is not None
        assert outcome.operation_result.verified is True
        assert outcome.operation_result.restored is False
        assert open(self.target, "rb").read() == self.new_content

    def test_auto_apply_rolls_back_when_validator_fails(self, monkeypatch):
        db.add_managed_file_target(self.user.id, self.target, "false")
        self._graduate(self.target)
        monkeypatch.setattr(operator_module.settings, "operator_enabled", True)

        outcome = operate_on_file(
            self.user.id, self.target, self.new_content,
            file_operator=self.operator,
        )
        assert outcome.auto_applied is True
        assert outcome.operation_result.verified is False
        assert outcome.operation_result.restored is True
        assert open(self.target, "rb").read() == b"original\n"

    def test_refuses_self_protection_target_regardless_of_gate(self, monkeypatch):
        protected = "/etc/systemd/system/rawos.service"
        db.add_managed_file_target(self.user.id, protected, "true")
        self._graduate(protected)
        monkeypatch.setattr(operator_module.settings, "operator_enabled", True)

        with pytest.raises(FileOperatorRefusalError):
            operate_on_file(
                self.user.id, protected, b"evil\n",
                file_operator=self.operator,
            )

    # --- Side-effect correctness ---

    def test_propose_only_does_not_update_track_record(self):
        operate_on_file(
            self.user.id, self.target, self.new_content,
            file_operator=self.operator,
        )
        record = db.get_operator_track_record(self.user.id, OPERATOR_CLASS, self.target)
        assert record.verified_successes == 0
        assert record.graduated is False

    def test_auto_apply_updates_track_record(self, monkeypatch):
        db.add_managed_file_target(self.user.id, self.target, "true")
        self._graduate(self.target)
        monkeypatch.setattr(operator_module.settings, "operator_enabled", True)

        operate_on_file(
            self.user.id, self.target, self.new_content,
            file_operator=self.operator,
        )
        record = db.get_operator_track_record(self.user.id, OPERATOR_CLASS, self.target)
        assert record.graduated is True


class TestExecuteApprovedFileEdit:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        db.init(os.path.join(self.tmp, "test.db"))
        self.user = db.create_user(User(
            email=f"approved-{id(self)}-{os.getpid()}@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))
        self.operator = LinuxFileOperator()
        self.target = os.path.join(self.tmp, "managed.conf")
        with open(self.target, "wb") as f:
            f.write(b"original\n")

    def test_runs_contract_and_applies(self):
        db.add_managed_file_target(self.user.id, self.target, "true")
        result = execute_approved_file_edit(
            self.user.id, self.target, b"approved content\n",
            file_operator=self.operator,
        )
        assert result.applied is True
        assert result.verified is True
        assert open(self.target, "rb").read() == b"approved content\n"

    def test_records_toward_graduation(self):
        db.add_managed_file_target(self.user.id, self.target, "true")
        # First approved apply → starts stability window (pending_since set)
        execute_approved_file_edit(
            self.user.id, self.target, b"v1\n",
            file_operator=self.operator,
        )
        # Reset file to simulate second edit
        with open(self.target, "wb") as f:
            f.write(b"v1\n")
        execute_approved_file_edit(
            self.user.id, self.target, b"v2\n",
            file_operator=self.operator,
        )
        record = db.get_operator_track_record(self.user.id, OPERATOR_CLASS, self.target)
        assert record.verified_successes == 1

    def test_raises_for_unregistered_target(self):
        with pytest.raises(OperatorError):
            execute_approved_file_edit(
                self.user.id, "/etc/never-added.conf", b"content\n",
                file_operator=self.operator,
            )

    def test_runs_without_operator_enabled_flag(self, monkeypatch):
        """execute_approved_file_edit does not check operator_enabled — owner explicitly approved."""
        db.add_managed_file_target(self.user.id, self.target, "true")
        monkeypatch.setattr(operator_module.settings, "operator_enabled", False)

        result = execute_approved_file_edit(
            self.user.id, self.target, b"owner approved\n",
            file_operator=self.operator,
        )
        assert result.applied is True
        assert result.verified is True
