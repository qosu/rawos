"""
rawos.kernel.venv_operator — M3 Stage 2: R-venv reversible dependency operator.

Provides the same earned-reversible-autonomy gate pattern as self_reload.py and
owned_resource.py, applied to the Python virtual-environment dependency layer.

## Why this exists
self_reload.py (R-self) intentionally refuses any commit that touches
pyproject.toml (I-SR5 — "dependency changes require a manual venv update and
restart"). That refusal exists because venv-state is not atomic like a git ref
— a bad `pip install` can break the runtime with no auto-revert path. R-venv
adds that missing safety-net: every dependency mutation is reversible via
rename-swap + deadman, exactly mirroring how R-self swaps source via git +
deadman.

## Reversibility discipline (I-VENV1)
  old venv → rename to .venvs/old-<id>        (preserved, pure-bash revertable)
  candidate → rename to venv                   (becomes live after restart)
  deadman (systemd-run) → fire if new self never proves healthy

The revert script (/usr/local/bin/rawos-venv-revert) is pure bash with zero
import-rawos / zero pip dependency (I-VENV1). This matches rawos-selfreload-revert
discipline exactly.

## Gate (I-VENV5)
  operate_on_venv()          — propose-only until operator_venv_enabled AND graduated
  execute_approved_venv_op() — owner path: bypass flag+graduation, keep preflight+deadman

## Invariants
  I-VENV1  Reversibility: rename-swap, pure-bash revert, ZERO pip-on-revert.
  I-VENV2  Preflight isolation: candidate build + prove before any live-venv touch.
  I-VENV3  Deadman: arm before rename; boot_venv_commit disarms on healthy /health.
  I-VENV4  Single-flight: pending state file blocks concurrent venv ops.
  I-VENV5  Gate dormant by default (operator_venv_enabled=False).
  I-VENV6  NO autonomous scan — reactive only (blast radius = no-boot).
  I-VENV7  Audit: every outcome → venv_operator_history ledger.
  I-VENV8  I-SR5 UNCHANGED — self_reload still refuses pyproject diff. This pass only.
  I-VENV9  _SELF_PROTECTED_SERVICES / TIER-0 / PAM / self_reload floors untouched.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, NoReturn

log = logging.getLogger("rawos.kernel.venv_operator")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: systemd timer unit for the venv deadman (mirrors SELF_RELOAD_DEADMAN_UNIT).
VENV_DEADMAN_UNIT: str = "rawos-venv-revert"

#: Seconds from arm until deadman fires if new self never proves healthy.
#: Generous (300 s) — venv-reinstall + import check on a slow box can be slow.
VENV_DEADMAN_DELAY_S: int = 300

#: Directory where the pending venv-swap state is persisted across restarts.
VENV_STATE_DIR: str = "/root/.rawos-venv-pending"

#: Filename within VENV_STATE_DIR.
VENV_STATE_FILENAME: str = "state.json"

#: Subdirectory under venv_root for staging and old-venv storage.
VENVS_SUBDIR: str = ".venvs"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class VenvRefusalError(Exception):
    """Unbypassable refusal — boundary or floor violation."""


class VenvPreflightError(Exception):
    """Candidate venv build / import-check / smoke-test failed. Live venv untouched."""


class VenvStateError(Exception):
    """Single-flight conflict or state-file corruption."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VenvDepSpec:
    """Specification for one dependency-update operation."""

    requirements: list[str]
    """pip-installable specs, e.g. ["requests==2.31", "httpx>=0.26"]. May be empty
    (used in tests / no-new-dep scenarios where the point is to rebuild the venv)."""

    op_type: str = "dep_update"
    """Audit ledger op_type label."""


@dataclass(frozen=True)
class VenvSnapshot:
    """Captures all information needed to complete or revert a venv swap."""

    state_id: str
    """UUID identifying this particular swap operation."""

    old_venv_id: str
    """Directory name (under .venvs/) where the live venv is moved before swap."""

    candidate_path: str
    """Absolute path of the candidate venv (under .venvs/candidate-<id>)."""

    frozen_hash_before: str
    """sha256 of ``pip freeze`` output from the live venv before swap."""

    frozen_hash_after: str
    """sha256 of ``pip freeze`` output from the candidate venv after install."""

    deadman_unit: str = VENV_DEADMAN_UNIT

    armed_at: float = field(default=0.0, compare=False)


@dataclass(frozen=True)
class VenvOperateOutcome:
    """Result of operate_on_venv / execute_approved_venv_op."""

    auto_applied: bool
    proposed: bool
    reason: str
    frozen_hash_after: str | None = None


# ---------------------------------------------------------------------------
# Real _VenvBuilder  (injectable for tests)
# ---------------------------------------------------------------------------

class _VenvBuilder:
    """Wraps subprocess calls for venv creation and validation."""

    def frozen_hash(self, venv_python: str) -> str:
        """sha256 of ``pip freeze`` output for the given venv's python."""
        result = subprocess.run(
            [venv_python, "-m", "pip", "freeze"],
            capture_output=True,
            text=True,
        )
        return hashlib.sha256(result.stdout.encode()).hexdigest()

    def create_venv(self, path: str) -> None:
        subprocess.check_call(
            ["/usr/bin/python3", "-m", "venv", "--copies", path]
        )

    def install_deps(
        self, venv_python: str, requirements: list[str]
    ) -> subprocess.CompletedProcess:
        if not requirements:
            return subprocess.CompletedProcess([], 0, "", "")
        return subprocess.run(
            [venv_python, "-m", "pip", "install"] + requirements,
            capture_output=True,
            text=True,
        )

    def check_import(
        self, venv_python: str, module: str
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [venv_python, "-c", f"import {module}"],
            capture_output=True,
            text=True,
        )

    def run_smoke(
        self, venv_python: str, cwd: str
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [venv_python, "-m", "pytest", "-q", "-m", "venv_smoke"],
            capture_output=True,
            text=True,
            cwd=cwd,
        )


# ---------------------------------------------------------------------------
# Real _VenvDeadmanSystemd  (injectable for tests)
# ---------------------------------------------------------------------------

class _VenvDeadmanSystemd:
    """Thin wrapper around systemd-run / systemctl for the venv revert timer.

    Mirrors _SelfReloadDeadmanSystemd in self_reload.py exactly.
    """

    def arm(self, unit: str, delay_s: int, revert_cmd: str) -> None:
        """Schedule revert_cmd to run in delay_s seconds via systemd-run."""
        subprocess.check_call(
            [
                "systemd-run",
                "--on-active",
                str(delay_s),
                f"--unit={unit}",
                "--",
                *revert_cmd.split(),
            ]
        )

    def disarm(self, unit: str) -> None:
        subprocess.run(
            ["systemctl", "stop", f"{unit}.timer"],
            capture_output=True,
            timeout=5.0,
        )


# ---------------------------------------------------------------------------
# preflight_venv
# ---------------------------------------------------------------------------

def preflight_venv(
    dep_spec: VenvDepSpec,
    *,
    venv_root: str,
    staging_root: str,
    _builder: object | None = None,
    _source_root: str | None = None,
) -> VenvSnapshot:
    """Build and prove a candidate venv in isolation; return a VenvSnapshot.

    Never touches the live venv at ``venv_root/venv``. If any step fails,
    the candidate directory is cleaned up and VenvPreflightError is raised.

    Order (mirrors preflight_stage in self_reload.py — I-VENV2):
      1. Compute frozen_hash_before from live venv
      2. Build candidate venv at staging_root/.venvs/candidate-<id>
      3. pip install dep_spec.requirements into candidate
      4. Prove: candidate/bin/python -c "import rawos.api.app"
      5. Prove: smoke pytest subset (marked venv_smoke)
      6. Compute frozen_hash_after from candidate
      7. Return VenvSnapshot
    """
    from rawos.config import settings as _s

    builder: _VenvBuilder = _builder or _VenvBuilder()
    source_root: str = _source_root or _s.rawos_source_root

    # 1. Snapshot live venv frozen deps hash
    live_venv_python = str(Path(venv_root) / "venv" / "bin" / "python")
    frozen_before = builder.frozen_hash(live_venv_python)

    # 2. Allocate candidate path
    state_id = str(uuid.uuid4())
    venvs_dir = Path(staging_root) / VENVS_SUBDIR
    venvs_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = str(venvs_dir / f"candidate-{state_id}")
    old_venv_id = f"old-{state_id}"

    # Cleanup helper — called on any failure
    def _cleanup() -> None:
        shutil.rmtree(candidate_path, ignore_errors=True)

    try:
        # 2. Build candidate venv
        builder.create_venv(candidate_path)
        cand_python = str(Path(candidate_path) / "bin" / "python")

        # 3. Install deps
        if dep_spec.requirements:
            install = builder.install_deps(cand_python, dep_spec.requirements)
            if install.returncode != 0:
                raise VenvPreflightError(
                    f"pip install failed in candidate venv: {install.stderr[-2000:]}"
                )

        # 4. Import check
        imp = builder.check_import(cand_python, "rawos.api.app")
        if imp.returncode != 0:
            raise VenvPreflightError(
                f"import check failed in candidate venv: {imp.stderr[-2000:]}"
            )

        # 5. Smoke subset
        smoke = builder.run_smoke(cand_python, source_root)
        if smoke.returncode != 0:
            raise VenvPreflightError(
                f"venv_smoke tests failed in candidate venv:\n{smoke.stdout[-2000:]}"
            )

        # 6. Frozen hash of candidate
        frozen_after = builder.frozen_hash(cand_python)

    except VenvPreflightError:
        _cleanup()
        raise
    except Exception as exc:
        _cleanup()
        raise VenvPreflightError(
            f"candidate venv preflight raised unexpected error: {exc}"
        ) from exc

    return VenvSnapshot(
        state_id=state_id,
        old_venv_id=old_venv_id,
        candidate_path=candidate_path,
        frozen_hash_before=frozen_before,
        frozen_hash_after=frozen_after,
        deadman_unit=VENV_DEADMAN_UNIT,
    )


# ---------------------------------------------------------------------------
# arm_and_swap_venv
# ---------------------------------------------------------------------------

def arm_and_swap_venv(
    snap: VenvSnapshot,
    *,
    venv_root: str,
    state_dir: str | None = None,
    _systemd: object | None = None,
    _exit: Callable[[int], None] = os._exit,
    _now: Callable[[], float] = time.time,
) -> NoReturn:  # type: ignore[return]
    """Arm the deadman, rename-swap venv to candidate, then kill this process.

    Order (I-VENV3/I-VENV4 — mirrors arm_and_swap in self_reload.py):
      1. Single-flight check — refuse if pending state file already exists
      2. Write state to disk (survives our death)
      3. Arm deadman (rawos-venv-revert, survives our death)
      4. Rename live venv → .venvs/old-<id>  (preserved for revert)
      5. Rename candidate → venv             (µs window, process still live in RAM)
      6. _exit(0) — systemd (Restart=always) respawns against new venv

    If step 4 or 5 raises: disarm deadman, clear state file, re-raise.
    _exit is NEVER called in that path — live venv is untouched (step 4 failed)
    or in intermediate state (step 5 failed; requires human inspection).
    """
    sd = _systemd or _VenvDeadmanSystemd()
    _state_dir = Path(state_dir or VENV_STATE_DIR)
    state_path = _state_dir / VENV_STATE_FILENAME

    # 1. Single-flight guard (I-VENV4)
    _state_dir.mkdir(parents=True, exist_ok=True)
    if state_path.exists():
        raise VenvStateError(
            f"refused: a venv swap is already pending ({state_path}). "
            "Resolve via boot_venv_commit before attempting another."
        )

    # 2. Write state (load-bearing — survives process death)
    armed_at = _now()
    record: dict = {
        "state_id": snap.state_id,
        "old_venv_id": snap.old_venv_id,
        "candidate_path": snap.candidate_path,
        "frozen_hash_before": snap.frozen_hash_before,
        "frozen_hash_after": snap.frozen_hash_after,
        "armed_at": armed_at,
        "deadman_unit": snap.deadman_unit,
    }
    state_path.write_text(json.dumps(record))

    # 3. Arm deadman BEFORE rename (I-VENV3)
    revert_cmd = f"/usr/local/bin/rawos-venv-revert {snap.state_id}"
    try:
        sd.arm(snap.deadman_unit, VENV_DEADMAN_DELAY_S, revert_cmd)
    except Exception:
        state_path.unlink(missing_ok=True)
        raise

    # 4+5. Rename-swap: old→.venvs/old-id then candidate→venv
    venv_path = Path(venv_root) / "venv"
    old_path = Path(venv_root) / VENVS_SUBDIR / snap.old_venv_id
    old_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        os.rename(str(venv_path), str(old_path))          # step 4
        os.rename(snap.candidate_path, str(venv_path))    # step 5
    except Exception:
        # Best-effort: disarm deadman, clear state. Live venv may be at
        # old_path (step 4 completed) or still at venv_path (step 4 failed).
        # Either way we do NOT attempt a programmatic rename-back here —
        # that risks double-rename confusion. Deadman disarm + state clear
        # prevent the revert-script from firing. Human inspection needed if
        # step 4 succeeded but step 5 failed (old_path → venv_path rename).
        sd.disarm(snap.deadman_unit)
        state_path.unlink(missing_ok=True)
        raise

    # 6. Exit — systemd Restart=always respawns against new venv
    _exit(0)


# ---------------------------------------------------------------------------
# boot_venv_commit
# ---------------------------------------------------------------------------

def boot_venv_commit(
    *,
    venv_root: str | None = None,
    state_dir: str | None = None,
    staging_root: str | None = None,
    _probe: Callable[[], bool],
    _systemd: object | None = None,
) -> str:
    """Resolve any pending venv swap at startup.

    Returns one of:
      "no_pending"       — no swap in flight, nothing to do.
      "committed"        — new venv passed /health; deadman disarmed, old venv reaped.
      "liveness_failed"  — /health not reached in time; deadman left armed (will revert).

    Mirrors boot_liveness_commit in self_reload.py. Called from the app.py
    lifespan task after the ASGI app is accepting requests.

    Never raises — liveness failure is expressed by leaving the deadman armed.
    The deadman itself will rename old_venv back and restart the service.
    """
    from rawos.config import settings as _s

    sd = _systemd or _VenvDeadmanSystemd()
    _state_dir = Path(state_dir or VENV_STATE_DIR)
    state_path = _state_dir / VENV_STATE_FILENAME
    _venv_root = venv_root or _s.rawos_source_root
    _staging_root = staging_root or _venv_root

    if not state_path.exists():
        return "no_pending"

    try:
        record = json.loads(state_path.read_text())
        old_venv_id = record["old_venv_id"]
        deadman_unit = record.get("deadman_unit", VENV_DEADMAN_UNIT)
    except Exception:
        log.exception("boot_venv_commit: state.json unreadable — leaving deadman armed")
        return "liveness_failed"

    if not _probe():
        log.warning("boot_venv_commit: /health probe failed — leaving deadman armed (will revert)")
        return "liveness_failed"

    # Healthy — disarm, reap old venv, clear state
    try:
        sd.disarm(deadman_unit)
    except Exception:
        log.exception("boot_venv_commit: failed to disarm %s", deadman_unit)

    old_venv_path = Path(_staging_root) / VENVS_SUBDIR / old_venv_id
    if old_venv_path.exists():
        try:
            shutil.rmtree(str(old_venv_path))
            log.info("boot_venv_commit: reaped old venv %s", old_venv_path)
        except Exception:
            log.exception("boot_venv_commit: failed to reap old venv %s", old_venv_path)

    try:
        state_path.unlink(missing_ok=True)
    except Exception:
        log.exception("boot_venv_commit: failed to clear state file")

    log.info("boot_venv_commit: committed — new venv is live and proven healthy")
    return "committed"


# ---------------------------------------------------------------------------
# operate_on_venv  (gate, mirrors operate_on_self_reload)
# ---------------------------------------------------------------------------

def operate_on_venv(
    user_id: str,
    dep_spec: VenvDepSpec,
    *,
    _venv_root: str | None = None,
    _staging_root: str | None = None,
    _builder: object | None = None,
    _source_root: str | None = None,
    _systemd: object | None = None,
    _exit: Callable[[int], None] = os._exit,
    _state_dir: str | None = None,
    _now: Callable[[], float] = time.time,
) -> VenvOperateOutcome:
    """Gate for autonomous / scheduled venv dependency update.

    Auto-applies (→ preflight + arm_and_swap_venv) iff ALL hold:
      1. settings.operator_venv_enabled is True
      2. operator_track_record class "venv_dep_update" is graduated

    Otherwise: propose-only (no side effects, no track-record write).

    I-VENV5: operator_venv_enabled ships False → always propose-only until
    owner flips the flag manually after gaining confidence.
    I-VENV6: this gate is never called autonomously in a scan loop. Reactive only.
    """
    import rawos.db as db
    from rawos.config import settings as _s

    venv_root = _venv_root or _s.rawos_source_root
    staging_root = _staging_root or venv_root

    if not _s.operator_venv_enabled:
        return VenvOperateOutcome(
            auto_applied=False,
            proposed=True,
            reason="operator_venv_enabled=False (dormant — owner must activate)",
        )

    track = db.get_operator_track_record(user_id, "venv_dep_update", venv_root)
    if not track.graduated:
        return VenvOperateOutcome(
            auto_applied=False,
            proposed=True,
            reason="venv_dep_update operation class not yet graduated",
        )

    execute_approved_venv_op(
        user_id,
        dep_spec,
        autonomous=True,
        _venv_root=venv_root,
        _staging_root=staging_root,
        _builder=_builder,
        _source_root=_source_root,
        _systemd=_systemd,
        _exit=_exit,
        _state_dir=_state_dir,
        _now=_now,
    )
    return VenvOperateOutcome(
        auto_applied=True,
        proposed=False,
        reason="preflight passed; armed and swapped to candidate venv (autonomous)",
    )


# ---------------------------------------------------------------------------
# execute_approved_venv_op  (owner path, mirrors execute_owner_self_reload)
# ---------------------------------------------------------------------------

def execute_approved_venv_op(
    user_id: str,
    dep_spec: VenvDepSpec,
    *,
    autonomous: bool = False,
    _venv_root: str | None = None,
    _staging_root: str | None = None,
    _builder: object | None = None,
    _source_root: str | None = None,
    _systemd: object | None = None,
    _exit: Callable[[int], None] = os._exit,
    _state_dir: str | None = None,
    _now: Callable[[], float] = time.time,
) -> VenvOperateOutcome:
    """Owner path: bypass flag + graduation, execute preflight + arm_and_swap.

    Still enforces:
      - preflight_venv (I-VENV2) — candidate must be proven before swap
      - arm_and_swap_venv deadman (I-VENV3) — revert-safe at all times
      - single-flight guard (I-VENV4)

    Raises VenvPreflightError if candidate fails proving.
    Raises VenvStateError if a swap is already in flight.
    """
    import rawos.db as _db
    from rawos.config import settings as _s

    venv_root = _venv_root or _s.rawos_source_root
    staging_root = _staging_root or venv_root

    snap = preflight_venv(
        dep_spec,
        venv_root=venv_root,
        staging_root=staging_root,
        _builder=_builder,
        _source_root=_source_root,
    )

    try:
        _db.record_venv_op_outcome(
            op_type=dep_spec.op_type,
            frozen_hash_before=snap.frozen_hash_before,
            frozen_hash_after=snap.frozen_hash_after,
            outcome="applied",
            autonomous=autonomous,
            deadman_unit=snap.deadman_unit,
        )
    except Exception:
        log.exception("execute_approved_venv_op: failed to write audit ledger (non-fatal)")

    arm_and_swap_venv(
        snap,
        venv_root=venv_root,
        state_dir=_state_dir,
        _systemd=_systemd,
        _exit=_exit,
        _now=_now,
    )
    # With real _exit this point is unreachable (NoReturn). With FakeExit it
    # returns so operate_on_venv can return the outcome.
    return VenvOperateOutcome(
        auto_applied=True,
        proposed=False,
        reason="preflight passed; armed and swapped (owner-approved)",
        frozen_hash_after=snap.frozen_hash_after,
    )
