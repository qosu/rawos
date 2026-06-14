"""tests/test_operator_service_action.py — TDD for ReversibleServiceAction (Phase 23a, Step 2).

A FakeServiceManager (in-memory active/inactive state, real start/stop/restart
semantics, no subprocess) stands in for the arch ServiceManager — live
systemctl verification happens on the box separately (see plan).
"""
from __future__ import annotations

import pytest

from rawos.kernel.operator import (
    OperationResult,
    OperatorError,
    ReversibleServiceAction,
    ServiceOperatorRefusalError,
    ServiceSnapshot,
    run_reversible_operation,
)


class FakeServiceManager:
    """In-memory ServiceManager double: tracks active/inactive run-state."""

    supports_reversible_apply = True
    supports_service_ops = True

    def __init__(self, *, initially_active: bool) -> None:
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


# ---------------------------------------------------------------------------
# Construction refusals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("protected_name", [
    "rawos.service", "rawos", "ssh.service", "ssh", "sshd.service", "sshd",
    "RAWOS.SERVICE", "SSH",
])
def test_refuses_self_protected_service_at_construction(protected_name):
    mgr = FakeServiceManager(initially_active=True)

    with pytest.raises(ServiceOperatorRefusalError):
        ReversibleServiceAction(mgr, protected_name, "restart", "true")


def test_refuses_invalid_action():
    mgr = FakeServiceManager(initially_active=True)

    with pytest.raises(OperatorError):
        ReversibleServiceAction(mgr, "rawos-svcprobe.service", "reload", "true")


def test_refuses_empty_validator_cmd():
    mgr = FakeServiceManager(initially_active=True)

    with pytest.raises(OperatorError):
        ReversibleServiceAction(mgr, "rawos-svcprobe.service", "restart", "")


# ---------------------------------------------------------------------------
# start — true run-state inverse
# ---------------------------------------------------------------------------

def test_start_keeps_on_validator_pass():
    mgr = FakeServiceManager(initially_active=False)
    op = ReversibleServiceAction(mgr, "rawos-svcprobe.service", "start", "true")

    result = run_reversible_operation(op)

    assert result == OperationResult(applied=True, verified=True, restored=False,
                                       detail="applied and verified")
    assert mgr.is_active("rawos-svcprobe.service") is True
    assert mgr.calls == ["start"]


def test_start_then_verify_fail_restores_by_stopping():
    mgr = FakeServiceManager(initially_active=False)
    op = ReversibleServiceAction(mgr, "rawos-svcprobe.service", "start", "false")

    result = run_reversible_operation(op)

    assert result.applied is True
    assert result.verified is False
    assert result.restored is True
    assert mgr.is_active("rawos-svcprobe.service") is False
    assert mgr.calls == ["start", "stop"]


# ---------------------------------------------------------------------------
# stop — true run-state inverse, verify uses is_active only (no validator run)
# ---------------------------------------------------------------------------

def test_stop_reaches_inactive_and_is_kept():
    mgr = FakeServiceManager(initially_active=True)
    op = ReversibleServiceAction(mgr, "rawos-svcprobe.service", "stop", "true")

    result = run_reversible_operation(op)

    assert result == OperationResult(applied=True, verified=True, restored=False,
                                       detail="applied and verified")
    assert mgr.is_active("rawos-svcprobe.service") is False
    assert mgr.calls == ["stop"]


def test_stop_verify_does_not_invoke_validator():
    """stop's oracle is is_active() alone — a stopped service can't pass a health check."""
    mgr = FakeServiceManager(initially_active=True)
    # validator_cmd="false" would fail if ever invoked for stop's verify
    op = ReversibleServiceAction(mgr, "rawos-svcprobe.service", "stop", "false")

    result = run_reversible_operation(op)

    assert result.verified is True
    assert result.restored is False


# ---------------------------------------------------------------------------
# restart — active->active; stated limitation: failed verify -> no-op restore
# ---------------------------------------------------------------------------

def test_restart_keeps_on_validator_pass():
    mgr = FakeServiceManager(initially_active=True)
    op = ReversibleServiceAction(mgr, "rawos-svcprobe.service", "restart", "true")

    result = run_reversible_operation(op)

    assert result == OperationResult(applied=True, verified=True, restored=False,
                                       detail="applied and verified")
    assert mgr.calls == ["restart"]


def test_restart_with_failing_validator_records_failure_with_noop_restore():
    mgr = FakeServiceManager(initially_active=True)
    op = ReversibleServiceAction(mgr, "rawos-svcprobe.service", "restart", "false")

    result = run_reversible_operation(op)

    assert result.applied is True
    assert result.verified is False
    assert result.restored is True
    # restore() ran but found is_active still True (matches was_active=True) -> no-op
    assert mgr.calls == ["restart"]
    assert mgr.is_active("rawos-svcprobe.service") is True


# ---------------------------------------------------------------------------
# capture() snapshot
# ---------------------------------------------------------------------------

def test_capture_returns_snapshot_of_current_state():
    mgr = FakeServiceManager(initially_active=True)
    op = ReversibleServiceAction(mgr, "rawos-svcprobe.service", "restart", "true")

    snapshot = op.capture()

    assert snapshot == ServiceSnapshot(service_name="rawos-svcprobe.service", was_active=True)
