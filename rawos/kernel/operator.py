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
from rawos.kernel.arch.base import FileOperator, FileSnapshot, ServiceManager

log = logging.getLogger("rawos.kernel.operator")

VALIDATOR_TIMEOUT_S = 30


class OperatorError(Exception):
    """Raised when an operator path refuses to run (safety precondition failed)."""


class ServiceOperatorRefusalError(Exception):
    """Raised when ReversibleServiceAction refuses a self-protected service.

    Self-protected services (rawos.service and the SSH daemon — never the
    being's own process or the owner's access path) can never be
    started/stopped/restarted through the operator, even if an owner
    allowlist entry would otherwise permit it. Mirrors
    arch.base.FileOperatorRefusalError for the service-lifecycle surface.
    """


_SELF_PROTECTED_SERVICES = frozenset({
    "rawos.service", "rawos",
    "ssh.service", "ssh",
    "sshd.service", "sshd",
})


@dataclass(frozen=True)
class ValidatorResult:
    passed: bool
    output: str


def run_validator(validator_cmd: str) -> ValidatorResult:
    """Run a target's validator command — the unfakeable health oracle.

    Returns passed=True with empty output on success (returncode 0).
    On failure (non-zero exit or timeout) returns passed=False with the
    captured stdout+stderr (or a timeout note) for diagnosis.
    """
    try:
        result = subprocess.run(
            validator_cmd, shell=True,
            capture_output=True, timeout=VALIDATOR_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        log.warning("run_validator: validator timed out: %s", validator_cmd)
        return ValidatorResult(passed=False, output="validator timed out")

    if result.returncode == 0:
        return ValidatorResult(passed=True, output="")

    output = (result.stdout + result.stderr).decode(errors="replace")
    return ValidatorResult(passed=False, output=output)


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
        return run_validator(self._validator_cmd).passed

    def restore(self, snapshot: FileSnapshot) -> None:
        self._operator.restore(snapshot)


@dataclass(frozen=True)
class ServiceSnapshot:
    """Captured pre-apply run-state of a service — the reversible state for
    ReversibleServiceAction. `was_active` is the systemd is-active verdict
    immediately before apply()."""
    service_name: str
    was_active: bool


_SERVICE_ACTIONS = ("restart", "start", "stop")


class ReversibleServiceAction:
    """Second ReversibleOperation instance: a machine service lifecycle
    action (R2) — restart, start, or stop a systemd unit via ServiceManager.

    capture()/restore() snapshot and restore the unit's run-state
    (active/inactive). apply() drives the requested action. verify() runs
    `validator_cmd` — the unfakeable health oracle — for restart/start
    (the service must be active AND pass the validator); for stop, the
    systemd is-active verdict alone is the oracle (a stopped service cannot
    pass a health check).

    KNOWN LIMITATION (stated, not hidden): for `restart` on an
    already-active service, if the restarted process is unhealthy the unit
    is still is_active=True, so restore() is a no-op — a bare restart's
    in-flight state cannot be un-restarted. The failed verify still records
    verified=False (no graduation credit) and surfaces the failure to the
    owner. `start` and `stop` are true run-state inverses with clean
    rollback.

    Refuses at construction:
      - a self-protected service (rawos.service / ssh / sshd, any form) —
        ServiceOperatorRefusalError (lockout-safety floor, never silent)
      - an action outside {restart, start, stop} — OperatorError
      - an empty validator_cmd — OperatorError (propose-only, can't verify)
    """

    def __init__(
        self,
        service_manager: ServiceManager,
        service_name: str,
        action: str,
        validator_cmd: str,
    ) -> None:
        if service_name.strip().lower() in _SELF_PROTECTED_SERVICES:
            raise ServiceOperatorRefusalError(
                f"refused: {service_name} is self-protected (lockout-safety floor)"
            )
        if action not in _SERVICE_ACTIONS:
            raise OperatorError(
                f"refusing to construct ReversibleServiceAction for {service_name}: "
                f"unknown action {action!r} — must be one of {_SERVICE_ACTIONS}"
            )
        if not validator_cmd:
            raise OperatorError(
                f"refusing to construct ReversibleServiceAction for {service_name}: "
                "no validator_cmd declared — target is propose-only"
            )
        self._manager = service_manager
        self.service_name = service_name
        self.action = action
        self._validator_cmd = validator_cmd

    def capture(self) -> ServiceSnapshot:
        return ServiceSnapshot(
            service_name=self.service_name,
            was_active=self._manager.is_active(self.service_name),
        )

    def apply(self) -> None:
        if self.action == "restart":
            self._manager.restart(self.service_name)
        elif self.action == "start":
            self._manager.start(self.service_name)
        else:  # "stop"
            self._manager.stop(self.service_name)

    def verify(self) -> bool:
        if self.action == "stop":
            return self._manager.is_active(self.service_name) is False
        return (
            self._manager.is_active(self.service_name) is True
            and run_validator(self._validator_cmd).passed
        )

    def restore(self, snapshot: ServiceSnapshot) -> None:
        is_active_now = self._manager.is_active(self.service_name)
        if snapshot.was_active and not is_active_now:
            self._manager.start(self.service_name)
        elif not snapshot.was_active and is_active_now:
            self._manager.stop(self.service_name)
        # else: already matches the pre-apply state (or a bare restart left
        # an active service active — the stated restart limitation), no-op.


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


# ---------------------------------------------------------------------------
# Phase 23a — service operator gate (R2, mirrors the R1 file gate above)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ServiceOperateOutcome:
    """Result of operate_on_service: either auto-applied or proposed."""
    auto_applied: bool
    proposed: bool
    operation_result: "OperationResult | None"
    proposed_action: "str | None"
    proposed_validator_cmd: "str | None"
    reason: str


def operate_on_service(
    user_id: str,
    service_name: str,
    action: str,
    *,
    service_manager: "ServiceManager | None" = None,
    now: "int | None" = None,
) -> ServiceOperateOutcome:
    """Single entry-point for managed service lifecycle actions (R2).

    Auto-applies iff ALL hold:
      1. settings.operator_service_enabled is True
      2. service_name in managed_service_targets allowlist for user_id
      3. (f"service_{action}", service_name) graduated (GRADUATION_THRESHOLD verified successes)

    On any condition failure: propose-only (no track-record write).
    ServiceOperatorRefusalError (self-protection) propagates unconditionally.
    """
    now_ = now if now is not None else int(time.time())
    operation_class = f"service_{action}"

    managed = db.get_managed_service_target(user_id, service_name)
    if managed is None:
        return ServiceOperateOutcome(
            auto_applied=False,
            proposed=True,
            operation_result=None,
            proposed_action=action,
            proposed_validator_cmd=None,
            reason="target not in managed_service_targets allowlist",
        )

    validator_cmd: str = managed["validator_cmd"]

    if not settings.operator_service_enabled:
        return ServiceOperateOutcome(
            auto_applied=False,
            proposed=True,
            operation_result=None,
            proposed_action=action,
            proposed_validator_cmd=validator_cmd,
            reason="operator_service_enabled=False",
        )

    track = db.get_operator_track_record(user_id, operation_class, service_name)
    if not track.graduated:
        return ServiceOperateOutcome(
            auto_applied=False,
            proposed=True,
            operation_result=None,
            proposed_action=action,
            proposed_validator_cmd=validator_cmd,
            reason="operation not yet graduated",
        )

    from rawos.kernel.arch import get_arch
    resolved_mgr = service_manager if service_manager is not None else get_arch().service_manager
    svc_op = ReversibleServiceAction(resolved_mgr, service_name, action, validator_cmd)
    result = run_reversible_operation(svc_op)  # ServiceOperatorRefusalError propagates
    db.update_operator_track_record(
        user_id, operation_class, service_name, verified=result.verified, now=now_,
    )
    return ServiceOperateOutcome(
        auto_applied=True,
        proposed=False,
        operation_result=result,
        proposed_action=None,
        proposed_validator_cmd=None,
        reason=result.detail,
    )


def execute_approved_service_action(
    user_id: str,
    service_name: str,
    action: str,
    *,
    service_manager: "ServiceManager | None" = None,
    now: "int | None" = None,
) -> OperationResult:
    """Execute an owner-approved service action: run full contract + record toward graduation.

    Does not check operator_service_enabled or graduation — the owner explicitly approved.
    ServiceOperatorRefusalError (self-protection floor) propagates unconditionally.
    """
    now_ = now if now is not None else int(time.time())
    operation_class = f"service_{action}"

    managed = db.get_managed_service_target(user_id, service_name)
    if managed is None:
        raise OperatorError(
            f"execute_approved_service_action: {service_name!r} not in managed_service_targets allowlist"
        )

    validator_cmd: str = managed["validator_cmd"]
    from rawos.kernel.arch import get_arch
    resolved_mgr = service_manager if service_manager is not None else get_arch().service_manager
    svc_op = ReversibleServiceAction(resolved_mgr, service_name, action, validator_cmd)
    result = run_reversible_operation(svc_op)
    db.update_operator_track_record(
        user_id, operation_class, service_name, verified=result.verified, now=now_,
    )
    return result


# ---------------------------------------------------------------------------
# Phase 23-full — Unit topology gate
# ---------------------------------------------------------------------------


# Re-export from unit_topology — same class, avoids dual exception hierarchy.
# Callers may import UnitTopologyRefusalError from rawos.kernel.operator or
# rawos.kernel.unit_topology — they are identical.
from rawos.kernel.unit_topology import UnitTopologyRefusalError  # noqa: F401


@dataclass(frozen=True)
class UnitTopologyOutcome:
    """Result of operate_on_unit_topology: either auto-applied or proposed."""

    auto_applied: bool
    proposed: bool
    operation_result: "OperationResult | None"
    proposed_op: "str | None"
    reason: str


def operate_on_unit_topology(
    user_id: str,
    unit_name: str,
    op: str,
    floor: "frozenset[str]",
    *,
    mgr: object = None,
    unit_content: "str | None" = None,
    target_name: "str | None" = None,
    now: "int | None" = None,
) -> UnitTopologyOutcome:
    """Single entry-point for unit topology operations (Phase 23-full).

    Gate order (deliberate — differs from operate_on_service):
      1. boot-graph op? → propose-only always (I-UT7, permanently human-gated).
      2. floor closure? → raise UnitTopologyRefusalError unconditionally (I-UT3).
      3. operator_unit_topology_enabled=False? → propose-only.
      4. unit_name not in managed_unit_targets? → propose-only.
      5. not yet graduated? → propose-only.
      6. auto-apply (runtime ops only).

    Boot-graph ops (enable/disable/set_default) NEVER auto-apply.
    UnitTopologyRefusalError propagates unconditionally before enabled check.
    """
    import time as _time
    from rawos.kernel import unit_topology as _ut
    from rawos.config import settings
    import rawos.db as _db

    now_ = now if now is not None else int(_time.time())
    operation_class = f"unit_topology_{op}"

    _BOOT_GRAPH_OPS = frozenset({"enable", "disable", "set_default"})

    # 1. Boot-graph op → propose-only always (I-UT7).
    if op in _BOOT_GRAPH_OPS:
        return UnitTopologyOutcome(
            auto_applied=False,
            proposed=True,
            operation_result=None,
            proposed_op=op,
            reason=(
                f"boot-graph op '{op}' is permanently propose-only (I-UT7); "
                "requires human-gated maintenance window"
            ),
        )

    # 2. Floor closure → raise unconditionally (I-UT3).
    # Instantiate action to let its constructor guard floor + op validity.
    # UnitTopologyRefusalError propagates unhandled.
    _ = _ut.ReversibleUnitTopologyAction(
        mgr, unit_name, op, floor,
        unit_content=unit_content,
        target_name=target_name,
    )

    # 3. Enabled check.
    if not settings.operator_unit_topology_enabled:
        return UnitTopologyOutcome(
            auto_applied=False,
            proposed=True,
            operation_result=None,
            proposed_op=op,
            reason="operator_unit_topology_enabled=False",
        )

    # 4. Allowlist check.
    managed = _db.get_managed_unit_target(user_id, unit_name)
    if managed is None:
        return UnitTopologyOutcome(
            auto_applied=False,
            proposed=True,
            operation_result=None,
            proposed_op=op,
            reason=f"unit {unit_name!r} not in managed_unit_targets allowlist",
        )

    # 5. Graduation check.
    track = _db.get_operator_track_record(user_id, operation_class, unit_name)
    if not track.graduated:
        return UnitTopologyOutcome(
            auto_applied=False,
            proposed=True,
            operation_result=None,
            proposed_op=op,
            reason=f"operation ({operation_class}, {unit_name!r}) not yet graduated",
        )

    # 6. Auto-apply.
    if mgr is not None:
        resolved_mgr = mgr
    else:
        from rawos.kernel.arch.linux import LinuxUnitTopologyManager
        resolved_mgr = LinuxUnitTopologyManager()
    action = _ut.ReversibleUnitTopologyAction(
        resolved_mgr, unit_name, op, floor,
        unit_content=unit_content,
        target_name=target_name,
    )
    result = run_reversible_operation(action)
    _db.update_operator_track_record(
        user_id, operation_class, unit_name, verified=result.verified, now=now_,
    )
    return UnitTopologyOutcome(
        auto_applied=True,
        proposed=False,
        operation_result=result,
        proposed_op=None,
        reason=result.detail,
    )


def execute_approved_unit_action(
    user_id: str,
    unit_name: str,
    op: str,
    floor: "frozenset[str]",
    *,
    mgr: object = None,
    unit_content: "str | None" = None,
    target_name: "str | None" = None,
    now: "int | None" = None,
) -> "OperationResult":
    """Execute an owner-approved unit topology action, bypassing graduation.

    For boot-graph ops: still applies UnitTopologyRefusalError floor check and
    default-target allowlist (I-UT3/I-UT4). No graduation required.
    Track-record written regardless (contributes toward graduation).
    """
    import time as _time
    from rawos.kernel import unit_topology as _ut
    import rawos.db as _db

    now_ = now if now is not None else int(_time.time())
    operation_class = f"unit_topology_{op}"

    if mgr is not None:
        resolved_mgr = mgr
    else:
        from rawos.kernel.arch.linux import LinuxUnitTopologyManager
        resolved_mgr = LinuxUnitTopologyManager()
    action = _ut.ReversibleUnitTopologyAction(
        resolved_mgr, unit_name, op, floor,
        unit_content=unit_content,
        target_name=target_name,
    )
    result = run_reversible_operation(action)
    _db.update_operator_track_record(
        user_id, operation_class, unit_name, verified=result.verified, now=now_,
    )
    return result
