"""rawos/kernel/owned_resource.py — M3: Owned-Resource Operator (R-own).

The being's first standing authority: lifecycle management over its own
namespace — workspaces, data artefacts, log rotation.

Invariants:
  I-OWN1  Boundary: every op passes is_owned_path() (realpath, no symlink/..
          escape); non-owned → OwnedResourceRefusalError at construction,
          unbypassable. System-level resources are unreachable.
  I-OWN2  Inner floor: even within owned roots, refuse ops on live DB files,
          .git, source code, and workspaces bound to active intents.
  I-OWN3  Reversibility: deletion = move-to-trash + manifest.json; hard-delete
          (reap) only after retention window.
  I-OWN4  Gate: auto-apply iff operator_owned_enabled AND op-class graduated;
          propose-only otherwise. execute_approved_owned_op bypasses both but
          still enforces I-OWN1 + I-OWN2.
  I-OWN5  Activation: only operator_owned_enabled is flipped after twin-prove;
          no other surface flags are touched.
  I-OWN6  Audit: every outcome written to owned_resource_history.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

log = logging.getLogger(__name__)

# ── public exception ────────────────────────────────────────────────────────

class OwnedResourceRefusalError(Exception):
    """Raised when a target violates the ownership boundary (I-OWN1) or inner
    floor (I-OWN2).  Always raised at the point of construction — never silently
    swallowed by the gate itself."""


# ── data types ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OwnedOpSpec:
    """Specification for one owned-resource operation."""
    op_type: str          # "workspace_gc" | "db_vacuum"
    target_path: str      # absolute path to the target resource
    trash_root: str | None = None  # override; defaults to source_root/data/.trash


@dataclass(frozen=True)
class GCResult:
    """Result of moving one workspace to trash."""
    original_path: str
    trash_path: str


@dataclass(frozen=True)
class OwnedOpOutcome:
    """Result of operate_on_owned_resource."""
    auto_applied: bool
    proposed: bool
    reason: str
    trash_path: str | None = None


@dataclass(frozen=True)
class OwnedApplyResult:
    """Result of execute_approved_owned_op."""
    applied: bool
    trash_path: str | None = None
    reason: str = ""


# ── DB filenames protected by I-OWN2 ────────────────────────────────────────

_PROTECTED_DB_FILENAMES: frozenset[str] = frozenset({
    "rawos.db",
    "rawos.db-wal",
    "rawos.db-shm",
})


# ══════════════════════════════════════════════════════════════════════════════
# OwnedResourceKernel
# ══════════════════════════════════════════════════════════════════════════════

class OwnedResourceKernel:
    """Owned-resource operator for one rawos installation.

    All paths are derived from (workspaces_root, rawos_source_root,
    worktree_root) — identical to config.py fields.  Tests inject fake roots
    via constructor; production uses get_default_kernel().

    Thread-safety: methods are stateless w.r.t. instance fields (all state is
    filesystem or DB). Concurrent calls are safe as long as callers serialize
    GC on the same target.
    """

    def __init__(
        self,
        *,
        workspaces_root: str,
        rawos_source_root: str,
        worktree_root: str,
    ) -> None:
        self._workspaces_root = os.path.realpath(workspaces_root)
        self._rawos_source_root = os.path.realpath(rawos_source_root)
        self._worktree_root = os.path.realpath(worktree_root)
        # rawos_source_root covers data/ + .git + source code.
        # Inner floor (I-OWN2) immediately refuses GC ops on source/git/DB.
        self._owned_roots: tuple[str, ...] = (
            self._workspaces_root,
            self._rawos_source_root,
            self._worktree_root,
        )

    # ── boundary ────────────────────────────────────────────────────────────

    def is_owned_path(self, path: str) -> bool:
        """True iff realpath(path) is inside one of the owned roots.

        Resolves symlinks and normalises `..` before checking — ensuring no
        escape via symlink or parent traversal (I-OWN1).
        """
        try:
            real = os.path.realpath(path)
        except (OSError, ValueError):
            return False
        for root in self._owned_roots:
            if real == root or real.startswith(root + os.sep):
                return True
        return False

    def assert_owned(self, path: str) -> None:
        """Raise OwnedResourceRefusalError if path is not in owned namespace."""
        if not self.is_owned_path(path):
            raise OwnedResourceRefusalError(
                f"target {path!r} is outside the owned namespace — "
                f"system-level resources are unreachable (I-OWN1)"
            )

    def assert_owned_and_not_floored(
        self,
        path: str,
        *,
        active_workspace_dirs: frozenset[str],
    ) -> None:
        """Enforce I-OWN1 (boundary) + I-OWN2 (inner floor).

        active_workspace_dirs — realpaths of workdir paths currently bound to
        non-terminal intents, as returned by db.get_active_workspace_dirs().
        Caller is responsible for querying and passing this set.
        """
        self.assert_owned(path)
        real = os.path.realpath(path)
        floored, reason = self._inner_floor_check(real, active_workspace_dirs)
        if floored:
            raise OwnedResourceRefusalError(
                f"target {path!r} is {reason} (I-OWN2)"
            )

    def _inner_floor_check(
        self,
        real_path: str,
        active_workspace_dirs: frozenset[str],
    ) -> tuple[bool, str]:
        """Return (is_floored, reason_phrase)."""
        p = Path(real_path)
        data_dir = os.path.join(self._rawos_source_root, "data")

        # 1. Live DB files (rawos.db, .db-wal, .db-shm)
        if p.name in _PROTECTED_DB_FILENAMES:
            return True, f"protected: live DB file {p.name!r}"

        # 2. .git directory anywhere inside owned namespace
        if ".git" in p.parts:
            return True, "protected: .git directory"

        # 3. Source code under rawos_source_root (not under rawos_source_root/data)
        if real_path == self._rawos_source_root or real_path.startswith(
            self._rawos_source_root + os.sep
        ):
            if not (
                real_path == data_dir
                or real_path.startswith(data_dir + os.sep)
            ):
                return True, "protected: rawos source code (use R1/self-reload surfaces)"

        # 4. Workspace bound to active (non-terminal) intent
        if real_path in active_workspace_dirs:
            return True, "active intent bound to workspace — cannot GC"

        return False, ""

    # ── lifecycle: workspace GC ──────────────────────────────────────────────

    def gc_workspace_to_trash(
        self,
        workspace_path: str,
        *,
        trash_root: str | None = None,
    ) -> GCResult:
        """Move workspace_path into trash_root/<ts>_<name>/ and write manifest.json.

        This is the ONLY path that removes a workspace from its original
        location (I-OWN3).  The trash entry is restorable via restore_from_trash
        until reap_trash hard-deletes it past the retention window.

        Caller must have already called assert_owned_and_not_floored().
        """
        effective_trash_root = trash_root or self._default_trash_root()
        ws = Path(workspace_path)
        ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        trash_entry = Path(effective_trash_root) / f"{ts}_{ws.name}"
        trash_entry.mkdir(parents=True, exist_ok=True)

        # Move workspace contents into trash entry dir
        ws_dest = trash_entry / ws.name
        shutil.move(str(ws), str(ws_dest))

        # Write manifest for restore
        manifest = {
            "original_path": str(ws),
            "trashed_at": int(time.time()),
            "workspace_name": ws.name,
            "contents_dir": ws.name,
        }
        (trash_entry / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        log.info(
            "owned GC: %r → trash %s", workspace_path, trash_entry
        )
        return GCResult(original_path=str(ws), trash_path=str(trash_entry))

    def restore_from_trash(self, trash_entry_path: str) -> None:
        """Restore a trashed workspace back to its original_path.

        Reads manifest.json to discover where to put it back.
        """
        trash_entry = Path(trash_entry_path)
        manifest_file = trash_entry / "manifest.json"
        if not manifest_file.exists():
            raise FileNotFoundError(
                f"No manifest.json in trash entry {trash_entry_path!r}"
            )
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        original_path = manifest["original_path"]
        contents_dir = manifest.get("contents_dir", Path(original_path).name)

        src = trash_entry / contents_dir
        dest = Path(original_path)
        if dest.exists():
            raise FileExistsError(
                f"Cannot restore: original path already exists: {original_path!r}"
            )
        shutil.move(str(src), str(dest))
        # Remove now-empty trash entry
        shutil.rmtree(str(trash_entry), ignore_errors=True)
        log.info("owned restore: %r ← trash %s", original_path, trash_entry_path)

    def reap_trash(
        self,
        *,
        trash_root: str | None = None,
        retention_days: int = 30,
    ) -> list[str]:
        """Hard-delete trash entries older than retention_days.

        This is the ONLY hard-delete path in the owned lifecycle (I-OWN3).
        Returns list of paths that were reaped.
        """
        effective_trash_root = trash_root or self._default_trash_root()
        trash_dir = Path(effective_trash_root)
        if not trash_dir.exists():
            return []

        cutoff = time.time() - retention_days * 86400
        reaped: list[str] = []

        for entry in trash_dir.iterdir():
            if not entry.is_dir():
                continue
            manifest_file = entry / "manifest.json"
            try:
                if manifest_file.exists():
                    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                    trashed_at = manifest.get("trashed_at", 0)
                else:
                    trashed_at = entry.stat().st_mtime
            except (OSError, json.JSONDecodeError, KeyError):
                trashed_at = 0

            if trashed_at < cutoff:
                shutil.rmtree(str(entry), ignore_errors=True)
                reaped.append(str(entry))
                log.info("owned reap: %s (trashed_at=%s)", entry, trashed_at)

        return reaped

    # ── lifecycle: DB maintenance ────────────────────────────────────────────

    def vacuum_db(self, db_path: str) -> None:
        """Run VACUUM + ANALYZE on a SQLite database file.

        Never deletes the file (I-OWN3).  Only safe sqlite3 API calls.
        Caller must ensure db_path is NOT rawos.db (inner floor prevents it).
        """
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("VACUUM")
            conn.execute("ANALYZE")
        finally:
            conn.close()
        log.info("owned db maintenance: VACUUM+ANALYZE %r", db_path)

    # ── gate: operate_on_owned_resource ─────────────────────────────────────

    def operate_on_owned_resource(
        self,
        user_id: str,
        op_spec: OwnedOpSpec,
        *,
        active_workspace_dirs: frozenset[str],
    ) -> OwnedOpOutcome:
        """Autonomous gate for owned-resource operations (mirrors operate_on_service).

        Auto-applies iff ALL hold:
          1. target passes I-OWN1 (boundary) + I-OWN2 (inner floor)
          2. settings.operator_owned_enabled is True
          3. op-class graduated (GRADUATION_THRESHOLD verified successes)

        On condition 2/3 failure: propose-only (no track-record write, no
        side effects).  I-OWN1/I-OWN2 refusals propagate unconditionally.
        """
        import rawos.db as db
        from rawos.config import settings

        # I-OWN1 + I-OWN2 (unbypassable)
        self.assert_owned_and_not_floored(
            op_spec.target_path,
            active_workspace_dirs=active_workspace_dirs,
        )

        if not settings.operator_owned_enabled:
            return OwnedOpOutcome(
                auto_applied=False,
                proposed=True,
                reason="operator_owned_enabled=False",
            )

        op_class, track_target = self._op_class_and_target(op_spec)
        track = db.get_operator_track_record(user_id, op_class, track_target)
        if not track.graduated:
            return OwnedOpOutcome(
                auto_applied=False,
                proposed=True,
                reason=f"{op_class} operation class not yet graduated",
            )

        # Auto-apply
        apply_result = self._apply_op(
            op_spec, active_workspace_dirs=active_workspace_dirs
        )
        return OwnedOpOutcome(
            auto_applied=True,
            proposed=False,
            reason="auto-applied (owned, enabled, graduated)",
            trash_path=apply_result.trash_path,
        )

    def execute_approved_owned_op(
        self,
        user_id: str,
        op_spec: OwnedOpSpec,
        *,
        active_workspace_dirs: frozenset[str],
    ) -> OwnedApplyResult:
        """Owner-approved path — bypasses operator_owned_enabled + graduation.

        Still enforces I-OWN1 (boundary) + I-OWN2 (inner floor) — these
        cannot be bypassed by anyone.
        """
        # I-OWN1 + I-OWN2 still enforced
        self.assert_owned_and_not_floored(
            op_spec.target_path,
            active_workspace_dirs=active_workspace_dirs,
        )
        return self._apply_op(op_spec, active_workspace_dirs=active_workspace_dirs)

    # ── internals ────────────────────────────────────────────────────────────

    def _op_class_and_target(self, op_spec: OwnedOpSpec) -> tuple[str, str]:
        """Return (operator_track_record_class, target) for graduation lookup."""
        if op_spec.op_type == "workspace_gc":
            return "owned_workspace_gc", self._workspaces_root
        if op_spec.op_type == "db_vacuum":
            return "owned_db_vacuum", self._rawos_source_root
        raise ValueError(f"unknown op_type: {op_spec.op_type!r}")

    def _apply_op(
        self,
        op_spec: OwnedOpSpec,
        *,
        active_workspace_dirs: frozenset[str],
    ) -> OwnedApplyResult:
        """Execute the operation.  Boundary + floor already verified by caller."""
        if op_spec.op_type == "workspace_gc":
            result = self.gc_workspace_to_trash(
                op_spec.target_path,
                trash_root=op_spec.trash_root,
            )
            return OwnedApplyResult(applied=True, trash_path=result.trash_path)
        if op_spec.op_type == "db_vacuum":
            self.vacuum_db(op_spec.target_path)
            return OwnedApplyResult(applied=True, reason="VACUUM+ANALYZE done")
        raise ValueError(f"unknown op_type: {op_spec.op_type!r}")

    def _default_trash_root(self) -> str:
        return str(Path(self._rawos_source_root) / "data" / ".trash")


# ── module-level convenience ─────────────────────────────────────────────────

def get_default_kernel() -> OwnedResourceKernel:
    """Return an OwnedResourceKernel derived from live settings."""
    from rawos.config import settings
    return OwnedResourceKernel(
        workspaces_root=settings.workspaces_root,
        rawos_source_root=settings.rawos_source_root,
        worktree_root=settings.worktree_root,
    )
