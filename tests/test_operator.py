"""tests/test_operator.py — TDD for the ReversibleOperation contract (Milestone 3, §7).

Real filesystem ops via LinuxFileOperator on tmp_path; validator commands are
real subprocesses ("true"/"false") — no mocking of the contract under test.
"""
from __future__ import annotations

import pytest

from rawos.kernel.arch.linux import LinuxFileOperator
from rawos.kernel.operator import (
    OperationResult,
    OperatorError,
    ReversibleFileEdit,
    run_reversible_operation,
)


def test_run_reversible_operation_keeps_change_when_verify_passes(tmp_path):
    operator = LinuxFileOperator()
    target = str(tmp_path / "config.conf")
    operator.write(target, b"original content\n")

    edit = ReversibleFileEdit(operator, target, b"new content\n", validator_cmd="true")
    result = run_reversible_operation(edit)

    assert result == OperationResult(applied=True, verified=True, restored=False,
                                       detail="applied and verified")
    assert operator.read(target) == b"new content\n"


def test_run_reversible_operation_restores_original_bytes_when_verify_fails(tmp_path):
    operator = LinuxFileOperator()
    target = str(tmp_path / "config.conf")
    operator.write(target, b"original content\n")

    edit = ReversibleFileEdit(operator, target, b"broken content\n", validator_cmd="false")
    result = run_reversible_operation(edit)

    assert result.applied is True
    assert result.verified is False
    assert result.restored is True
    assert operator.read(target) == b"original content\n"


def test_run_reversible_operation_refuses_op_without_restore(tmp_path):
    operator = LinuxFileOperator()
    target = str(tmp_path / "config.conf")
    operator.write(target, b"original content\n")

    class NoRestoreOp:
        """Stub missing restore() — irreversible, can never be auto-run."""

        def capture(self):
            return None

        def apply(self) -> None:
            operator.write(target, b"changed\n")

        def verify(self) -> bool:
            return True

    with pytest.raises(OperatorError):
        run_reversible_operation(NoRestoreOp())


def test_reversible_file_edit_refuses_no_validator_target(tmp_path):
    operator = LinuxFileOperator()
    target = str(tmp_path / "config.conf")
    operator.write(target, b"original content\n")

    with pytest.raises(OperatorError):
        ReversibleFileEdit(operator, target, b"new content\n", validator_cmd="")


def test_reversible_file_edit_apply_propagates_self_protection_refusal():
    from rawos.kernel.arch.base import FileOperatorRefusalError

    operator = LinuxFileOperator()
    edit = ReversibleFileEdit(
        operator, "/etc/systemd/system/rawos.service", b"malicious\n", validator_cmd="true",
    )

    with pytest.raises(FileOperatorRefusalError):
        edit.apply()
