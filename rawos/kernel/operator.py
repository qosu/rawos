"""kernel/operator — the ReversibleOperation contract (Milestone 3, §7).

Generalizes reversible_apply.py's git-specific capture/apply/verify/restore
shape into a Protocol any machine-operation can implement.
ReversibleFileEdit is the first instance: managed file edits (R1) via the
arch FileOperator.

reversible_apply.py (the git/R2 instance) is NOT refactored onto this
contract in this milestone — it remains the sibling git/R2 instance the
contract was generalized from (see Milestone 3 plan).
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Protocol

import time

import rawos.db as db
from rawos.config import settings
from rawos.kernel.arch.base import FileOperator, FileSnapshot

log = logging.getLogger("rawos.kernel.operator")

VALIDATOR_TIMEOUT_S = 30


class OperatorError(Exception):
    """Raised when an operator path refuses to run (safety precondition failed)."""


class ReversibleOperation(Protocol):
    """capture() -> Snapshot, apply(), verify() -> bool, restore(Snapshot).

    An operation that does not implement both capture and restore can never
    be run via run_reversible_operation (irreversible-floor).
    """

    def capture(self): ...
    def apply(self) -> None: ...
    def verify(self) -> bool: ...
    def restore(self, snapshot) -> None: ...


@dataclass(frozen=True)
class OperationResult:
    applied: bool
    verified: bool
    restored: bool
    detail: str


def run_reversible_operation(operation: ReversibleOperation) -> OperationResult:
    """capture -> apply -> verify -> keep | restore.

    Raises OperatorError if `operation` does not implement capture/restore
    (irreversible-floor: an op without restore can never be auto-run).
    """
    if not (hasattr(operation, "capture") and hasattr(operation, "restore")):
        raise OperatorError(
            f"{type(operation).__name__} does not implement capture/restore "
            "— cannot run as a reversible operation (irreversible-floor)"
        )

    snapshot = operation.capture()
    operation.apply()

    try:
        verified = operation.verify()
    except Exception:
        log.exception("run_reversible_operation: verify() raised for %s", type(operation).__name__)
        verified = False

    if verified:
        return OperationResult(applied=True, verified=True, restored=False,
                                detail="applied and verified")

    operation.restore(snapshot)
    return OperationResult(applied=True, verified=False, restored=True,
                            detail="verify failed — restored to pre-apply state")


class ReversibleFileEdit:
    """First ReversibleOperation instance: a managed file edit (R1).

    capture()/restore() delegate to FileOperator.backup/restore (snapshot
    roundtrip, including self-protection refusals). apply() writes
    `new_content` via FileOperator.write. verify() runs `validator_cmd` —
    the unfakeable oracle (e.g. "nginx -t") — exit code 0 == pass.

    A target with no validator_cmd cannot be verified: such targets are
    propose-only (kernel.operator gate) and can never graduate, so
    construction is refused here rather than allowed to silently roll back.
    """

    def __init__(
        self,
        file_operator: FileOperator,
        target_path: str,
        new_content: bytes,
        validator_cmd: str,
    ) -> None:
        if not validator_cmd:
            raise OperatorError(
                f"refusing to construct ReversibleFileEdit for {target_path}: "
                "no validator_cmd declared — target is propose-only"
            )
        self._operator = file_operator
        self.target_path = target_path
        self._new_content = new_content
        self._validator_cmd = validator_cmd

    def capture(self) -> FileSnapshot:
        return self._operator.backup(self.target_path)

    def apply(self) -> None:
        self._operator.write(self.target_path, self._new_content)

    def verify(self) -> bool:
        try:
            result = subprocess.run(
                self._validator_cmd, shell=True,
                capture_output=True, timeout=VALIDATOR_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            log.warning("ReversibleFileEdit.verify: validator timed out: %s", self._validator_cmd)
            return False
        return result.returncode == 0

    def restore(self, snapshot: FileSnapshot) -> None:
        self._operator.restore(snapshot)


@dataclass(frozen=True)
class OperateOutcome:
    """Result of operate_on_file: either auto-applied or proposed."""
    auto_applied: bool
    proposed: bool
    operation_result: "OperationResult | None"
    proposed_content: "bytes | None"
    proposed_validator_cmd: "str | None"
    reason: str


def operate_on_file(
    user_id: str,
    target_path: str,
    new_content: bytes,
    *,
    operation_class: str = "file_edit",
    file_operator: "FileOperator | None" = None,
    now: "int | None" = None,
) -> OperateOutcome:
    """Single entry-point for managed file edits (R1).

    Auto-applies iff ALL hold:
      1. settings.operator_enabled is True
      2. target_path in managed_file_targets allowlist for user_id
      3. (operation_class, target_path) graduated (GRADUATION_THRESHOLD verified successes)

    On any condition failure: propose-only (no track-record write).
    Self-protection violations (FileOperatorRefusalError) propagate unconditionally.
    """
    now_ = now if now is not None else int(time.time())

    managed = db.get_managed_file_target(user_id, target_path)
    if managed is None:
        return OperateOutcome(
            auto_applied=False,
            proposed=True,
            operation_result=None,
            proposed_content=new_content,
            proposed_validator_cmd=None,
            reason="target not in managed_file_targets allowlist",
        )

    validator_cmd: str = managed["validator_cmd"]

    if not settings.operator_enabled:
        return OperateOutcome(
            auto_applied=False,
            proposed=True,
            operation_result=None,
            proposed_content=new_content,
            proposed_validator_cmd=validator_cmd,
            reason="operator_enabled=False",
        )

    track = db.get_operator_track_record(user_id, operation_class, target_path)
    if not track.graduated:
        return OperateOutcome(
            auto_applied=False,
            proposed=True,
            operation_result=None,
            proposed_content=new_content,
            proposed_validator_cmd=validator_cmd,
            reason="operation not yet graduated",
        )

    from rawos.kernel.arch import get_arch  # local import avoids circular at module load
    resolved_operator = file_operator if file_operator is not None else get_arch().file_operator
    edit = ReversibleFileEdit(resolved_operator, target_path, new_content, validator_cmd)
    result = run_reversible_operation(edit)  # FileOperatorRefusalError propagates
    db.update_operator_track_record(
        user_id, operation_class, target_path, verified=result.verified, now=now_,
    )
    return OperateOutcome(
        auto_applied=True,
        proposed=False,
        operation_result=result,
        proposed_content=None,
        proposed_validator_cmd=None,
        reason=result.detail,
    )


def execute_approved_file_edit(
    user_id: str,
    target_path: str,
    new_content: bytes,
    *,
    operation_class: str = "file_edit",
    file_operator: "FileOperator | None" = None,
    now: "int | None" = None,
) -> OperationResult:
    """Execute an owner-approved file edit: run full contract + record toward graduation.

    Does not check operator_enabled or graduation — the owner explicitly approved.
    """
    now_ = now if now is not None else int(time.time())

    managed = db.get_managed_file_target(user_id, target_path)
    if managed is None:
        raise OperatorError(
            f"execute_approved_file_edit: {target_path!r} not in managed_file_targets allowlist"
        )

    validator_cmd: str = managed["validator_cmd"]
    from rawos.kernel.arch import get_arch
    resolved_operator = file_operator if file_operator is not None else get_arch().file_operator
    edit = ReversibleFileEdit(resolved_operator, target_path, new_content, validator_cmd)
    result = run_reversible_operation(edit)
    db.update_operator_track_record(
        user_id, operation_class, target_path, verified=result.verified, now=now_,
    )
    return result
