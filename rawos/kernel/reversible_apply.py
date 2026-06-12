"""
rawos reversible auto-apply — Stage 3 of "Earned, Reversible Autonomy"
(see docs/plans/squishy-watching-stroustrup.md).

Applies a graduated (repo, anomaly_domain) class's proposed rawos/fix-*
branch directly to a repo's live tree, restarts the affected systemd
service, and health-gates the result: if the service is not healthy
within HEALTH_GATE_TIMEOUT_S, automatically rolls back to the pre-apply
commit and restarts again. Never pushes — every apply/rollback moves
local HEAD only, so the audit trail (before_sha/after_sha in ApplyResult,
plus the caller's autonomy_track_record/episodic_memory entries) fully
reconstructs what happened.

Refuses to operate on rawos's own source tree
(kernel.tools._is_rawos_source_tree) — this module must never restart or
roll back the process running it.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from rawos.kernel.arch import get_arch
from rawos.kernel.sandbox import SandboxError, run_bash
from rawos.kernel.tools import _is_rawos_source_tree

log = logging.getLogger("rawos.kernel.reversible_apply")

HEALTH_GATE_TIMEOUT_S = 60
HEALTH_GATE_POLL_INTERVAL_S = 2.0


class ReversibleApplyError(Exception):
    """Raised when reversible_apply refuses to run (safety precondition failed)."""


@dataclass(frozen=True)
class ApplyResult:
    applied: bool      # fast-forward merge to fix_branch succeeded
    healthy: bool       # health_check returned True within timeout_s after restart
    rolled_back: bool   # before_sha was restored (because not healthy, or restart failed)
    before_sha: str | None
    after_sha: str | None
    detail: str


async def reversible_apply(
    repo_root: str,
    fix_branch: str,
    service_name: str,
    *,
    health_check: Callable[[], Awaitable[bool]],
    timeout_s: int = HEALTH_GATE_TIMEOUT_S,
    poll_interval_s: float = HEALTH_GATE_POLL_INTERVAL_S,
) -> ApplyResult:
    """Fast-forward `repo_root` to `fix_branch`, restart `service_name`, health-gate.

    On any failure after a successful fast-forward (restart fails, or
    health_check stays False for timeout_s), `repo_root` is reset back to
    its pre-apply HEAD and `service_name` is restarted again.
    """
    arch = get_arch()
    if not arch.service_manager.supports_reversible_apply:
        raise ReversibleApplyError(
            f"arch backend does not support reversible_apply "
            f"(supports_reversible_apply=False): {type(arch.service_manager).__name__}"
        )

    if await _is_rawos_source_tree(repo_root):
        raise ReversibleApplyError(
            f"refusing to reversible_apply against rawos's own source tree: {repo_root}"
        )

    try:
        before = await run_bash("git rev-parse HEAD", repo_root)
    except SandboxError as exc:
        return ApplyResult(False, False, False, None, None, f"git rev-parse HEAD raised: {exc}")
    if before.exit_code != 0:
        return ApplyResult(False, False, False, None, None,
                            f"git rev-parse HEAD failed: {before.stderr.strip()}")
    before_sha = before.stdout.strip()

    try:
        merge = await run_bash(f"git merge --ff-only {fix_branch}", repo_root)
    except SandboxError as exc:
        return ApplyResult(False, False, False, before_sha, None, f"git merge raised: {exc}")
    if merge.exit_code != 0:
        return ApplyResult(False, False, False, before_sha, None,
                            f"fast-forward merge of {fix_branch} failed: "
                            f"{(merge.stderr or merge.stdout).strip()}")

    after = await run_bash("git rev-parse HEAD", repo_root)
    after_sha = after.stdout.strip()

    loop = asyncio.get_running_loop()
    restart_ok = await loop.run_in_executor(
        None, arch.service_manager.restart, service_name,
    )
    if not restart_ok:
        await _rollback(repo_root, service_name, before_sha, arch)
        return ApplyResult(True, False, True, before_sha, after_sha,
                            f"systemctl restart {service_name} failed")

    elapsed = 0.0
    while elapsed < timeout_s:
        await asyncio.sleep(poll_interval_s)
        elapsed += poll_interval_s
        try:
            healthy = await health_check()
        except Exception:
            log.exception("reversible_apply: health_check raised for %s", service_name)
            healthy = False
        if healthy:
            return ApplyResult(True, True, False, before_sha, after_sha,
                                f"applied {fix_branch} ({before_sha[:8]} -> {after_sha[:8]}), "
                                f"{service_name} healthy after {elapsed:.0f}s")

    await _rollback(repo_root, service_name, before_sha, arch)
    return ApplyResult(True, False, True, before_sha, after_sha,
                        f"applied {fix_branch} ({before_sha[:8]} -> {after_sha[:8]}) but "
                        f"{service_name} not healthy within {timeout_s}s — rolled back to {before_sha[:8]}")


async def _rollback(repo_root: str, service_name: str, before_sha: str, arch=None) -> None:
    if arch is None:
        arch = get_arch()

    try:
        reset = await run_bash(f"git reset --hard {before_sha}", repo_root)
        if reset.exit_code != 0:
            log.error("reversible_apply: rollback git reset --hard %s failed in %s: %s",
                       before_sha, repo_root, reset.stderr.strip())
    except SandboxError:
        log.exception("reversible_apply: rollback git reset --hard raised in %s", repo_root)

    loop = asyncio.get_running_loop()
    try:
        restart_ok = await loop.run_in_executor(
            None, arch.service_manager.restart, service_name,
        )
        if not restart_ok:
            log.error("reversible_apply: rollback systemctl restart %s failed", service_name)
    except Exception:
        log.exception("reversible_apply: rollback systemctl restart %s raised", service_name)
