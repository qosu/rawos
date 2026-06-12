"""
kernel/reversible_apply — wired to kernel/arch ServiceManager ABI.

Characterization:
1. supports_reversible_apply gate: if backend.service_manager.supports_reversible_apply
   is False, reversible_apply() must raise ReversibleApplyError immediately.
2. restart wiring: the systemctl restart call must go through
   get_arch().service_manager.restart(service_name) (via run_in_executor),
   NOT through run_bash("systemctl restart ...").
Stage A: zero behavior change on Linux — LinuxServiceManager.supports_reversible_apply
is True, so the gate never fires on Linux. Restart behavior is identical.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from rawos.kernel.reversible_apply import ReversibleApplyError, reversible_apply
from rawos.kernel.sandbox import BashResult


def _ok_bash(stdout: str = "", stderr: str = "") -> BashResult:
    return BashResult(stdout=stdout, stderr=stderr, exit_code=0, duration_ms=1, truncated=False)


def _fail_bash(stderr: str = "err") -> BashResult:
    return BashResult(stdout="", stderr=stderr, exit_code=1, duration_ms=1, truncated=False)


def _mock_arch(supports: bool = True, restart_ok: bool = True) -> MagicMock:
    backend = MagicMock()
    backend.service_manager.supports_reversible_apply = supports
    backend.service_manager.restart.return_value = restart_ok
    return backend


# ---------------------------------------------------------------------------
# Gate test
# ---------------------------------------------------------------------------

def test_reversible_apply_gate_raises_if_backend_does_not_support():
    """supports_reversible_apply=False must cause immediate ReversibleApplyError."""
    backend = _mock_arch(supports=False)

    with patch("rawos.kernel.reversible_apply.get_arch", return_value=backend), \
         patch("rawos.kernel.reversible_apply._is_rawos_source_tree",
               new=AsyncMock(return_value=False)):
        with pytest.raises(ReversibleApplyError, match="supports_reversible_apply"):
            asyncio.run(reversible_apply(
                "/tmp/repo", "rawos/fix-x", "rawos.service",
                health_check=AsyncMock(return_value=True),
            ))


# ---------------------------------------------------------------------------
# Restart wiring test
# ---------------------------------------------------------------------------

def test_reversible_apply_wires_restart_to_service_manager(tmp_path):
    """restart must call service_manager.restart(), not run_bash('systemctl restart ...')."""
    before_sha = "deadbeef" * 5

    # run_bash side-effect: map by command prefix
    def _fake_run_bash(cmd: str, _workdir: str) -> BashResult:
        if cmd.startswith("git rev-parse HEAD"):
            return _ok_bash(stdout=before_sha + "\n")
        if cmd.startswith("git merge"):
            return _ok_bash()
        if cmd.startswith("systemctl"):
            # Must never be reached — restart must go through service_manager
            raise AssertionError(f"run_bash must NOT call systemctl directly: {cmd!r}")
        return _ok_bash()

    backend = _mock_arch(supports=True, restart_ok=True)

    async def _healthy() -> bool:
        return True

    with patch("rawos.kernel.reversible_apply.get_arch", return_value=backend), \
         patch("rawos.kernel.reversible_apply._is_rawos_source_tree",
               new=AsyncMock(return_value=False)), \
         patch("rawos.kernel.reversible_apply.run_bash",
               new=AsyncMock(side_effect=_fake_run_bash)):
        result = asyncio.run(reversible_apply(
            str(tmp_path), "rawos/fix-x", "rawos.service",
            health_check=_healthy,
        ))

    assert result.applied is True
    assert result.healthy is True
    assert result.rolled_back is False
    backend.service_manager.restart.assert_called_once_with("rawos.service")


def test_reversible_apply_rolls_back_when_restart_fails(tmp_path):
    """restart returning False must trigger rollback + service_manager.restart again."""
    before_sha = "aabbccdd" * 5

    def _fake_run_bash(cmd: str, _workdir: str) -> BashResult:
        if cmd.startswith("git rev-parse HEAD"):
            return _ok_bash(stdout=before_sha + "\n")
        if cmd.startswith("git merge"):
            return _ok_bash()
        if cmd.startswith("git reset"):
            return _ok_bash()
        if cmd.startswith("systemctl"):
            raise AssertionError(f"run_bash must NOT call systemctl directly: {cmd!r}")
        return _ok_bash()

    backend = _mock_arch(supports=True, restart_ok=False)

    async def _healthy() -> bool:
        return True

    with patch("rawos.kernel.reversible_apply.get_arch", return_value=backend), \
         patch("rawos.kernel.reversible_apply._is_rawos_source_tree",
               new=AsyncMock(return_value=False)), \
         patch("rawos.kernel.reversible_apply.run_bash",
               new=AsyncMock(side_effect=_fake_run_bash)):
        result = asyncio.run(reversible_apply(
            str(tmp_path), "rawos/fix-x", "rawos.service",
            health_check=_healthy,
        ))

    assert result.applied is True
    assert result.rolled_back is True
    # restart called twice: once for apply, once in _rollback
    assert backend.service_manager.restart.call_count == 2
