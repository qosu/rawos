"""rawos/kernel/self_reload.py — Phase 25 Stage 1: safe self-reload (R-self).

The Ouroboros. The being can already AUTHOR a new version of its own source
(operator R1 can target /root/rawos, which is rawos.service's WorkingDirectory)
but cannot BECOME it — rawos.service restart is hard-floored in
_SELF_PROTECTED_SERVICES (kernel/operator.py) and every proactive path
explicitly forbids self-restart. This module closes that joint *safely*,
without relaxing the floor: instead of `systemctl restart rawos` (which stays
floored), the current process simply dies (os._exit(0)); systemd
(Restart=always, RestartUSec=5s) respawns it ~5s later against whatever
source is on disk.

Safety invariants (Stage 1):
  I-SR1  Narrow dedicated path. The operator floor (rawos/ssh/sshd in
         _SELF_PROTECTED_SERVICES) is never touched. Additionally, preflight
         refuses to change a fixed set of TIER-0 files that define rawos's
         own safety mechanisms (this module, operator.py, pam_operator.py) —
         self-reload cannot rewrite its own guardrails.
  I-SR2  Deadman armed before swap; swap before exit. If any pre-exit step
         after arming raises, disarm + leave source untouched + re-raise.
         Never exit into an un-armed swapped state.
  I-SR3  Revert runs in systemd root context via a standalone script with no
         rawos import (/usr/local/bin/rawos-selfreload-revert, mirrors
         rawos-pam-restore).
  I-SR4  Migration-monotonicity refusal: preflight refuses ANY diff under
         migrations/ between old_sha and new_sha. Self-reload changes code,
         never schema — schema changes go through human-merge + manual
         restart, as today.
  I-SR5  venv-frozen refusal: preflight refuses if dependency-declaration
         files (pyproject.toml) differ between old_sha and new_sha. Pure-
         Python source only; a self-edit needing a new package degrades to
         propose-only.
  I-SR6  No autonomous path. This module exposes no `operate_on_self_reload`
         symbol. self_reload_enabled defaults to False.
  I-SR7  Liveness oracle is unfakeable and self-run: boot_liveness_commit
         must observe the NEW process actually serving (probe callable
         supplied by the caller — /health, frontdoor, DB read, schema).
  I-SR8  Single-flight: a pending state file refuses a second concurrent
         arm_and_swap (resolve the first one via boot_liveness_commit before
         starting another).

Stage 2 ("M6-for-self", autonomous self-sovereignty) adds the following.
I-SR6 is superseded (this module now exposes `operate_on_self_reload`), but
the entrypoint is inert until BOTH new gates open:
  I-SR9  operate_on_self_reload(user_id) auto-applies (autonomous=True) iff
         settings.self_reload_enabled is True AND the operator_track_record
         class "self_reload" for target=SOURCE_ROOT is graduated
         (GRADUATION_THRESHOLD verified successes — earned-reversible-autonomy,
         same ladder as operate_on_service). Otherwise propose-only, no
         side effects, no track-record write. If master HEAD == current HEAD
         (nothing to become), it is a true no-op (neither auto-applied nor
         proposed). Preflight refusals (I-SR1/I-SR4/I-SR5) and single-flight
         (I-SR8) propagate unconditionally — autonomy needs no new safety
         machinery.
  I-SR10 settings.self_reload_autonomous_enabled (default False) is the
         second, independent gate the proactive loop checks before even
         calling operate_on_self_reload — belt-and-suspenders: flipping
         self_reload_enabled alone (e.g. for a one-off owner-triggered
         arm-and-go) does not also enable the autonomous loop path.
  I-SR11 Every boot_liveness_commit() resolution ("committed" / "resurrected"
         / "liveness_failed") is recorded both in the managed_self_reload
         ledger (with the `autonomous` flag carried in pending.json from
         arm time) AND as one operator_track_record update for class
         "self_reload" target=SOURCE_ROOT (verified=True only on
         "committed") — this is the graduation signal I-SR9 reads.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NoReturn

from rawos.config import settings

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SOURCE_ROOT = settings.rawos_source_root

SELF_RELOAD_STATE_DIR = settings.self_reload_state_dir
SELF_RELOAD_STATE_FILENAME = "pending.json"
SELF_RELOAD_WORKTREE_ROOT = "/root/.rawos-selfreload-worktrees"

SELF_RELOAD_DEADMAN_UNIT = "rawos-selfreload-revert"
# Calibrated on-box per the plan's twin-verification procedure (§Verification):
# DELAY = cold-boot-to-/health-200 p99 + 4x margin. 180s is the documented
# starting bias-long estimate pending that calibration.
SELF_RELOAD_DEADMAN_DELAY_S = 180
SELF_RELOAD_LIVENESS_MARGIN_S = 30
SELF_RELOAD_PROBE_INTERVAL_S = 2.0

# Dependency-declaration files (I-SR5). Any diff here between old_sha and
# new_sha refuses the reload — self-reload is pure-Python source only.
_DEPENDENCY_FILES: tuple[str, ...] = ("pyproject.toml",)

# TIER-0: files that define rawos's own safety floors. A self-reload that
# would change any of these is refused outright (I-SR1) — the being cannot
# rewrite its own guardrails via the unattended reload path.
_TIER0_PROTECTED_FILES: frozenset[str] = frozenset({
    "rawos/kernel/self_reload.py",
    "rawos/kernel/operator.py",
    "rawos/kernel/pam_operator.py",
})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class SelfReloadRefusalError(Exception):
    """Preflight refused this reload outright (I-SR1/I-SR4/I-SR5). Unbypassable."""


class SelfReloadPreflightError(RuntimeError):
    """Preflight staging (import check / smoke-test subset) failed."""


class SelfReloadStateError(RuntimeError):
    """Invalid self-reload state: single-flight violation, corrupt state, or
    a git operation (rev-parse/reset) failed."""


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SelfReloadSnapshot:
    old_sha: str
    new_sha: str
    state_id: str
    armed_at: float
    deadman_unit: str
    migration_delta: list[str]
    venv_frozen_hash: str


# ---------------------------------------------------------------------------
# Injectable collaborators
# ---------------------------------------------------------------------------

class _GitRunner:
    """Thin subprocess wrapper. Injected as `_runner` so tests never shell out."""

    def run(self, args: list[str], cwd: str) -> subprocess.CompletedProcess:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=120)


class _WorktreeManager:
    """Disposable detached-HEAD worktree at a specific SHA, for preflight staging.

    Deliberately separate from kernel/worktree.py: that module runs git via
    run_bash() inside the sandboxed Docker container (--network none, only
    /workspace mounted) and has no access to /root/rawos on the host. Preflight
    must inspect the HOST source tree, so it shells out directly.
    """

    def create(self, repo_path: str, sha: str) -> str:
        root = Path(SELF_RELOAD_WORKTREE_ROOT)
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"selfreload-{sha[:12]}-{uuid.uuid4().hex[:8]}"
        result = subprocess.run(
            ["git", "worktree", "add", "--detach", str(target), sha],
            cwd=repo_path, capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise SelfReloadPreflightError(f"git worktree add failed: {result.stderr.strip()}")
        return str(target)

    def remove(self, worktree_path: str) -> None:
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_path],
            cwd=SOURCE_ROOT, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            shutil.rmtree(worktree_path, ignore_errors=True)


class _SelfReloadDeadmanSystemd:
    """Mirrors frontdoor._DeadmanSystemd / pam_operator._PamDeadmanSystemd."""

    def arm(self, unit: str, delay_s: int, revert_cmd: str) -> None:
        subprocess.run(
            ["systemd-run", "--on-active", str(delay_s), f"--unit={unit}", "--", *revert_cmd.split()],
            check=True, capture_output=True, timeout=10.0,
        )

    def disarm(self, unit: str) -> None:
        subprocess.run(["systemctl", "stop", f"{unit}.timer"], capture_output=True, timeout=5.0)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git_head(runner, cwd: str) -> str:
    return _git_rev_parse(runner, cwd, "HEAD")


def _git_rev_parse(runner, cwd: str, ref: str) -> str:
    result = runner.run(["git", "rev-parse", ref], cwd=cwd)
    if result.returncode != 0:
        raise SelfReloadStateError(f"git rev-parse {ref} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _diff_paths(runner, cwd: str, old_sha: str, new_sha: str, pathspec: str) -> list[str]:
    args = ["git", "diff", "--name-only", f"{old_sha}..{new_sha}"]
    if pathspec:
        args += ["--", pathspec]
    result = runner.run(args, cwd=cwd)
    if result.returncode != 0:
        raise SelfReloadStateError(f"git diff failed: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line.strip()]


def _dependency_hash(runner, cwd: str, sha: str) -> str:
    h = hashlib.sha256()
    for fname in _DEPENDENCY_FILES:
        result = runner.run(["git", "show", f"{sha}:{fname}"], cwd=cwd)
        content = result.stdout if result.returncode == 0 else ""
        h.update(content.encode())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# preflight_stage
# ---------------------------------------------------------------------------

def preflight_stage(
    new_sha: str,
    *,
    _source_root: str | None = None,
    _runner: object | None = None,
    _worktree: object | None = None,
) -> SelfReloadSnapshot:
    """Stage new_sha in an isolated worktree and prove it is safe to become.

    Refuses outright (SelfReloadRefusalError, I-SR1/I-SR4/I-SR5) without ever
    touching a worktree if:
      - migrations/ differs between HEAD and new_sha (schema change)
      - any TIER-0 protected file differs (self-reload would rewrite its own
        guardrails)
      - pyproject.toml differs (dependency/venv drift)

    Otherwise stages new_sha in a disposable detached-HEAD worktree and
    requires `python3 -c "import rawos.api.app"` and the
    `self_reload_smoke` pytest marker subset to both succeed
    (SelfReloadPreflightError on failure).

    Returns a SelfReloadSnapshot WITHOUT arming anything or crossing the
    restart boundary — purely diagnostic up to this point.
    """
    source_root = _source_root or SOURCE_ROOT
    runner = _runner or _GitRunner()

    old_sha = _git_head(runner, source_root)

    migration_delta = _diff_paths(runner, source_root, old_sha, new_sha, "migrations/")
    if migration_delta:
        raise SelfReloadRefusalError(
            f"refused: migrations/ would change ({migration_delta}). "
            "Self-reload is code-only (I-SR4); schema changes require "
            "human-merge + manual restart."
        )

    all_changed = _diff_paths(runner, source_root, old_sha, new_sha, "")
    tier0_hits = sorted(set(all_changed) & _TIER0_PROTECTED_FILES)
    if tier0_hits:
        raise SelfReloadRefusalError(
            f"refused: TIER-0 protected files would change ({tier0_hits}). "
            "Self-reload cannot modify its own safety mechanism (I-SR1)."
        )

    old_dep_hash = _dependency_hash(runner, source_root, old_sha)
    new_dep_hash = _dependency_hash(runner, source_root, new_sha)
    if old_dep_hash != new_dep_hash:
        raise SelfReloadRefusalError(
            "refused: dependency declaration (pyproject.toml) would change. "
            "Self-reload is pure-Python source only (I-SR5); dependency "
            "changes require a manual venv update and restart."
        )

    worktree = _worktree or _WorktreeManager()
    wt_path = worktree.create(source_root, new_sha)
    try:
        imp = runner.run(["python3", "-c", "import rawos.api.app"], cwd=wt_path)
        if imp.returncode != 0:
            raise SelfReloadPreflightError(f"preflight import check failed: {imp.stderr.strip()}")

        test = runner.run(["python3", "-m", "pytest", "-q", "-m", "self_reload_smoke"], cwd=wt_path)
        if test.returncode != 0:
            raise SelfReloadPreflightError(
                f"preflight self_reload_smoke test subset failed:\n{test.stdout[-2000:]}"
            )
    finally:
        worktree.remove(wt_path)

    return SelfReloadSnapshot(
        old_sha=old_sha,
        new_sha=new_sha,
        state_id=str(uuid.uuid4()),
        armed_at=0.0,
        deadman_unit=SELF_RELOAD_DEADMAN_UNIT,
        migration_delta=migration_delta,
        venv_frozen_hash=new_dep_hash,
    )


# ---------------------------------------------------------------------------
# arm_and_swap
# ---------------------------------------------------------------------------

def arm_and_swap(
    snap: SelfReloadSnapshot,
    *,
    autonomous: bool = False,
    _systemd: object | None = None,
    _exit: Callable[[int], None] = os._exit,
    _source_root: str | None = None,
    _runner: object | None = None,
    _state_dir: str | None = None,
    _now: Callable[[], float] = time.time,
    _revert_cmd: str | None = None,
) -> NoReturn:
    """Arm the deadman, swap source to new_sha, then kill this process.

    Order (I-SR2, invariant):
      1. single-flight check — refuse if a pending state file already exists
      2. write state to disk (survives our death)
      3. arm deadman (rawos-selfreload-revert, survives our death)
      4. git reset --hard new_sha
      5. _exit(0) — systemd (Restart=always) respawns against new_sha

    If writing state or arming raises: nothing was swapped, state file (if
    written) is removed, exception propagates — caller is unchanged.

    If the git reset fails (step 4): disarm the deadman, remove the state
    file, raise SelfReloadStateError. _exit is NEVER called in this path —
    source is untouched, so there is nothing to revert and no reason to die.
    """
    sd = _systemd or _SelfReloadDeadmanSystemd()
    runner = _runner or _GitRunner()
    source_root = _source_root or SOURCE_ROOT
    state_dir = Path(_state_dir or SELF_RELOAD_STATE_DIR)
    state_path = state_dir / SELF_RELOAD_STATE_FILENAME

    state_dir.mkdir(parents=True, exist_ok=True)
    if state_path.exists():
        raise SelfReloadStateError(
            f"refused: a self-reload is already pending ({state_path}). "
            "Single-flight (I-SR8) — resolve it via boot_liveness_commit first."
        )

    armed_at = _now()
    record = {
        "old_sha": snap.old_sha,
        "new_sha": snap.new_sha,
        "state_id": snap.state_id,
        "armed_at": armed_at,
        "deadman_unit": snap.deadman_unit,
        "autonomous": autonomous,
    }
    state_path.write_text(json.dumps(record))

    revert_cmd = _revert_cmd or f"/usr/local/bin/rawos-selfreload-revert {snap.old_sha} {snap.state_id}"
    try:
        sd.arm(snap.deadman_unit, SELF_RELOAD_DEADMAN_DELAY_S, revert_cmd)
    except Exception:
        state_path.unlink(missing_ok=True)
        raise

    try:
        result = runner.run(["git", "reset", "--hard", snap.new_sha], cwd=source_root)
        if result.returncode != 0:
            raise SelfReloadStateError(
                f"git reset --hard {snap.new_sha} failed: {result.stderr.strip()}"
            )
    except Exception:
        sd.disarm(snap.deadman_unit)
        state_path.unlink(missing_ok=True)
        raise

    _exit(0)


# ---------------------------------------------------------------------------
# boot_liveness_commit
# ---------------------------------------------------------------------------

def boot_liveness_commit(
    *,
    _systemd: object | None = None,
    _probe: Callable[[], bool] | None = None,
    _now: Callable[[], float] = time.time,
    _sleep: Callable[[float], None] = time.sleep,
    _source_root: str | None = None,
    _runner: object | None = None,
    _state_dir: str | None = None,
) -> str:
    """Resolve any pending self-reload at boot. The out-of-band verify step.

    Returns one of:
      "no_pending"       — nothing to resolve
      "committed"        — HEAD == new_sha and the probe passed before the
                            deadman deadline; deadman disarmed, state cleared
      "resurrected"      — HEAD == old_sha (the deadman already reverted us
                            and systemd respawned this prior-proven-good
                            self); deadman disarmed (idempotent), state cleared
      "liveness_failed"  — HEAD == new_sha but the probe never passed before
                            the deadline; deadman is NOT disarmed (left armed
                            so it fires and reverts us), state retained

    The probe is retried at SELF_RELOAD_PROBE_INTERVAL_S until
    armed_at + SELF_RELOAD_DEADMAN_DELAY_S - SELF_RELOAD_LIVENESS_MARGIN_S, so
    a slow warmup() never false-fails (I-SR7).
    """
    sd = _systemd or _SelfReloadDeadmanSystemd()
    runner = _runner or _GitRunner()
    source_root = _source_root or SOURCE_ROOT
    state_dir = Path(_state_dir or SELF_RELOAD_STATE_DIR)
    state_path = state_dir / SELF_RELOAD_STATE_FILENAME

    if not state_path.exists():
        return "no_pending"

    record = json.loads(state_path.read_text())
    old_sha = record["old_sha"]
    new_sha = record["new_sha"]
    deadman_unit = record["deadman_unit"]
    armed_at = record["armed_at"]

    current_head = _git_head(runner, source_root)

    if current_head == old_sha:
        sd.disarm(deadman_unit)
        state_path.unlink(missing_ok=True)
        return "resurrected"

    if current_head != new_sha:
        # Source under us matches neither side of the pending reload — leave
        # the deadman armed (it will revert us to a known-good old_sha) and
        # retain state for post-mortem.
        return "liveness_failed"

    probe = _probe or (lambda: False)
    deadline = armed_at + SELF_RELOAD_DEADMAN_DELAY_S - SELF_RELOAD_LIVENESS_MARGIN_S

    while True:
        if probe():
            sd.disarm(deadman_unit)
            state_path.unlink(missing_ok=True)
            return "committed"
        if _now() >= deadline:
            return "liveness_failed"
        _sleep(SELF_RELOAD_PROBE_INTERVAL_S)


# ---------------------------------------------------------------------------
# Owner-triggered funnel (I-SR6: the ONLY entrypoint in Stage 1)
# ---------------------------------------------------------------------------

def execute_owner_self_reload(
    new_sha: str,
    *,
    autonomous: bool = False,
    _source_root: str | None = None,
    _runner: object | None = None,
    _worktree: object | None = None,
    _systemd: object | None = None,
    _exit: Callable[[int], None] = os._exit,
    _state_dir: str | None = None,
    _now: Callable[[], float] = time.time,
) -> NoReturn:
    """preflight_stage(new_sha) -> arm_and_swap(snapshot). The single funnel.

    Stage 2's operate_on_self_reload() calls this same funnel unchanged
    (autonomous=True) after a self-improve branch merges to master — no
    second arm/swap path. `autonomous` is carried into pending.json so the
    boot-commit task can record it on the managed_self_reload ledger row
    (I-SR11).
    """
    snap = preflight_stage(new_sha, _source_root=_source_root, _runner=_runner, _worktree=_worktree)
    arm_and_swap(
        snap, autonomous=autonomous,
        _systemd=_systemd, _exit=_exit,
        _source_root=_source_root, _runner=_runner, _state_dir=_state_dir, _now=_now,
    )


# ---------------------------------------------------------------------------
# Stage 2 — autonomous gate (I-SR9, I-SR10)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SelfReloadOperateOutcome:
    """Result of operate_on_self_reload: auto-applied, proposed, or no-op."""
    auto_applied: bool
    proposed: bool
    new_sha: str | None
    reason: str


def operate_on_self_reload(
    user_id: str,
    *,
    _source_root: str | None = None,
    _runner: object | None = None,
    _worktree: object | None = None,
    _systemd: object | None = None,
    _exit: Callable[[int], None] = os._exit,
    _state_dir: str | None = None,
    _now: Callable[[], float] = time.time,
) -> SelfReloadOperateOutcome:
    """Stage 2 autonomous gate (mirrors operator.operate_on_service).

    Auto-applies (execute_owner_self_reload(new_sha, autonomous=True)) iff
    ALL hold:
      1. master HEAD != current HEAD (there is something new to become —
         otherwise this is a true no-op, neither applied nor proposed)
      2. settings.self_reload_enabled is True
      3. operator_track_record class "self_reload", target=SOURCE_ROOT is
         graduated (GRADUATION_THRESHOLD verified successes, written by the
         boot-commit task per I-SR11)

    On any condition 2/3 failure: propose-only (no track-record write, no
    side effects). SelfReloadRefusalError / SelfReloadPreflightError /
    SelfReloadStateError (preflight refusals, single-flight) propagate
    unconditionally — autonomy needs no new safety machinery (I-SR1/I-SR4/
    I-SR5/I-SR8 apply identically).
    """
    import rawos.db as db

    source_root = _source_root or SOURCE_ROOT
    runner = _runner or _GitRunner()

    old_sha = _git_head(runner, source_root)
    new_sha = _git_rev_parse(runner, source_root, "master")

    if new_sha == old_sha:
        return SelfReloadOperateOutcome(
            auto_applied=False, proposed=False, new_sha=None,
            reason="master HEAD == current HEAD; nothing to reload",
        )

    if not settings.self_reload_enabled:
        return SelfReloadOperateOutcome(
            auto_applied=False, proposed=True, new_sha=new_sha,
            reason="self_reload_enabled=False",
        )

    track = db.get_operator_track_record(user_id, "self_reload", source_root)
    if not track.graduated:
        return SelfReloadOperateOutcome(
            auto_applied=False, proposed=True, new_sha=new_sha,
            reason="self_reload operation class not yet graduated",
        )

    execute_owner_self_reload(
        new_sha,
        autonomous=True,
        _source_root=_source_root, _runner=_runner, _worktree=_worktree,
        _systemd=_systemd, _exit=_exit, _state_dir=_state_dir, _now=_now,
    )
    return SelfReloadOperateOutcome(
        auto_applied=True, proposed=False, new_sha=new_sha,
        reason="armed and swapped to master HEAD (autonomous)",
    )
