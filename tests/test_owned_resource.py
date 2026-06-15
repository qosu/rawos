"""tests/test_owned_resource.py — M3 R-own: owned-resource operator (I-OWN1..6)."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest


# ── fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def owned_roots(tmp_path):
    """Create fake owned roots mirroring rawos config layout."""
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    source = tmp_path / "source"
    data = source / "data"
    data.mkdir(parents=True)
    worktrees = tmp_path / ".rawos-worktrees"
    worktrees.mkdir()
    return {
        "workspaces_root": str(workspaces),
        "rawos_source_root": str(source),
        "worktree_root": str(worktrees),
    }


@pytest.fixture()
def kernel(owned_roots):
    """OwnedResourceKernel with injectable test roots."""
    from rawos.kernel.owned_resource import OwnedResourceKernel
    return OwnedResourceKernel(
        workspaces_root=owned_roots["workspaces_root"],
        rawos_source_root=owned_roots["rawos_source_root"],
        worktree_root=owned_roots["worktree_root"],
    )


# ══════════════════════════════════════════════════════════════════════════════
# I-OWN1: Ownership boundary — is_owned_path + assert_owned
# ══════════════════════════════════════════════════════════════════════════════

class TestOwnedBoundary:
    def test_path_inside_workspaces_is_owned(self, kernel, owned_roots):
        p = Path(owned_roots["workspaces_root"]) / "abc123"
        p.mkdir()
        assert kernel.is_owned_path(str(p)) is True

    def test_path_inside_data_is_owned(self, kernel, owned_roots):
        data = Path(owned_roots["rawos_source_root"]) / "data"
        assert kernel.is_owned_path(str(data)) is True

    def test_path_inside_worktrees_is_owned(self, kernel, owned_roots):
        wt = Path(owned_roots["worktree_root"]) / "some-branch"
        wt.mkdir()
        assert kernel.is_owned_path(str(wt)) is True

    def test_path_outside_all_roots_is_not_owned(self, kernel):
        assert kernel.is_owned_path("/etc/passwd") is False

    def test_path_outside_raises_refusal_error(self, kernel):
        from rawos.kernel.owned_resource import OwnedResourceRefusalError
        with pytest.raises(OwnedResourceRefusalError):
            kernel.assert_owned("/etc/passwd")

    def test_parent_traversal_refused(self, kernel, owned_roots):
        """Path using .. to escape owned root must be refused."""
        from rawos.kernel.owned_resource import OwnedResourceRefusalError
        traversal = owned_roots["workspaces_root"] + "/../../../etc"
        with pytest.raises(OwnedResourceRefusalError):
            kernel.assert_owned(traversal)

    def test_symlink_escape_refused(self, kernel, owned_roots):
        """Symlink inside owned root pointing outside must be refused."""
        from rawos.kernel.owned_resource import OwnedResourceRefusalError
        link = Path(owned_roots["workspaces_root"]) / "evil-link"
        link.symlink_to("/etc")
        with pytest.raises(OwnedResourceRefusalError):
            kernel.assert_owned(str(link))


# ══════════════════════════════════════════════════════════════════════════════
# I-OWN2: Inner floor — protected targets even within owned namespace
# ══════════════════════════════════════════════════════════════════════════════

class TestInnerFloor:
    def test_live_db_file_refused(self, kernel, owned_roots):
        from rawos.kernel.owned_resource import OwnedResourceRefusalError
        db_file = Path(owned_roots["rawos_source_root"]) / "data" / "rawos.db"
        db_file.touch()
        with pytest.raises(OwnedResourceRefusalError, match="protected"):
            kernel.assert_owned_and_not_floored(str(db_file), active_workspace_dirs=frozenset())

    def test_db_wal_refused(self, kernel, owned_roots):
        from rawos.kernel.owned_resource import OwnedResourceRefusalError
        wal = Path(owned_roots["rawos_source_root"]) / "data" / "rawos.db-wal"
        wal.touch()
        with pytest.raises(OwnedResourceRefusalError, match="protected"):
            kernel.assert_owned_and_not_floored(str(wal), active_workspace_dirs=frozenset())

    def test_db_shm_refused(self, kernel, owned_roots):
        from rawos.kernel.owned_resource import OwnedResourceRefusalError
        shm = Path(owned_roots["rawos_source_root"]) / "data" / "rawos.db-shm"
        shm.touch()
        with pytest.raises(OwnedResourceRefusalError, match="protected"):
            kernel.assert_owned_and_not_floored(str(shm), active_workspace_dirs=frozenset())

    def test_git_dir_refused(self, kernel, owned_roots):
        from rawos.kernel.owned_resource import OwnedResourceRefusalError
        git_dir = Path(owned_roots["rawos_source_root"]) / ".git"
        git_dir.mkdir()
        with pytest.raises(OwnedResourceRefusalError, match="protected"):
            kernel.assert_owned_and_not_floored(str(git_dir), active_workspace_dirs=frozenset())

    def test_source_py_file_refused(self, kernel, owned_roots):
        """Source code under rawos_source_root (non-data) must be inner-floored."""
        from rawos.kernel.owned_resource import OwnedResourceRefusalError
        src = Path(owned_roots["rawos_source_root"]) / "rawos" / "kernel"
        src.mkdir(parents=True)
        py = src / "foo.py"
        py.touch()
        with pytest.raises(OwnedResourceRefusalError, match="protected"):
            kernel.assert_owned_and_not_floored(str(py), active_workspace_dirs=frozenset())

    def test_stale_workspace_not_floored(self, kernel, owned_roots):
        """Stale workspace with no active intent must NOT be inner-floored."""
        ws = Path(owned_roots["workspaces_root"]) / "stale-workspace"
        ws.mkdir()
        # passes with empty active set
        kernel.assert_owned_and_not_floored(str(ws), active_workspace_dirs=frozenset())

    def test_active_intent_workspace_refused(self, kernel, owned_roots):
        """Workspace bound to active intent must be inner-floored."""
        from rawos.kernel.owned_resource import OwnedResourceRefusalError
        ws = Path(owned_roots["workspaces_root"]) / "active-ws"
        ws.mkdir()
        with pytest.raises(OwnedResourceRefusalError, match="active intent"):
            kernel.assert_owned_and_not_floored(
                str(ws),
                active_workspace_dirs=frozenset([str(ws)]),
            )

    def test_trash_dir_not_floored(self, kernel, owned_roots):
        """data/.trash is owned data space — must be accessible for trash writes."""
        data = Path(owned_roots["rawos_source_root"]) / "data"
        trash = data / ".trash"
        trash.mkdir(parents=True)
        # must NOT raise — trash is the reversibility mechanism
        kernel.assert_owned_and_not_floored(str(trash), active_workspace_dirs=frozenset())


# ══════════════════════════════════════════════════════════════════════════════
# I-OWN3: Reversibility — GC moves to trash, manifest, restore, reap
# ══════════════════════════════════════════════════════════════════════════════

class TestWorkspaceGC:
    def _make_workspace(self, workspaces_root: str, name: str, age_days: float = 0) -> Path:
        ws = Path(workspaces_root) / name
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "artifact.txt").write_text("data")
        if age_days > 0:
            mtime = time.time() - age_days * 86400
            os.utime(ws, (mtime, mtime))
        return ws

    def test_gc_moves_to_trash_not_hard_deletes(self, kernel, owned_roots):
        ws = self._make_workspace(owned_roots["workspaces_root"], "old-ws", age_days=30)
        trash_root = Path(owned_roots["rawos_source_root"]) / "data" / ".trash"

        result = kernel.gc_workspace_to_trash(str(ws), trash_root=str(trash_root))

        assert not ws.exists(), "workspace must be gone from original location"
        assert result.trash_path is not None
        assert Path(result.trash_path).exists(), "trash dir must exist"

    def test_gc_writes_manifest(self, kernel, owned_roots):
        ws = self._make_workspace(owned_roots["workspaces_root"], "old-ws2", age_days=30)
        trash_root = Path(owned_roots["rawos_source_root"]) / "data" / ".trash"

        result = kernel.gc_workspace_to_trash(str(ws), trash_root=str(trash_root))

        manifest = Path(result.trash_path) / "manifest.json"
        assert manifest.exists()
        data = json.loads(manifest.read_text())
        assert data["original_path"] == str(ws)
        assert "trashed_at" in data

    def test_trash_restorable(self, kernel, owned_roots):
        ws = self._make_workspace(owned_roots["workspaces_root"], "restore-ws", age_days=30)
        trash_root = Path(owned_roots["rawos_source_root"]) / "data" / ".trash"

        result = kernel.gc_workspace_to_trash(str(ws), trash_root=str(trash_root))
        assert not ws.exists()

        kernel.restore_from_trash(result.trash_path)

        assert ws.exists(), "workspace must be back after restore"
        assert (ws / "artifact.txt").exists(), "content must be preserved"

    def test_reap_only_past_retention_window(self, kernel, owned_roots):
        """Hard-delete must only fire for trash older than retention_days."""
        trash_root = Path(owned_roots["rawos_source_root"]) / "data" / ".trash"
        trash_root.mkdir(parents=True)

        # Fresh trash entry — must NOT be reaped
        fresh = trash_root / "20991231T000000_fresh"
        fresh.mkdir()
        (fresh / "manifest.json").write_text(
            json.dumps({"trashed_at": int(time.time())})
        )

        # Old trash entry (40 days ago, past 30-day window) — MUST be reaped
        old = trash_root / "20200101T000000_old"
        old.mkdir()
        old_time = time.time() - 40 * 86400
        (old / "manifest.json").write_text(
            json.dumps({"trashed_at": int(old_time)})
        )
        os.utime(old, (old_time, old_time))

        reaped = kernel.reap_trash(trash_root=str(trash_root), retention_days=30)

        assert not old.exists(), "old trash must be reaped"
        assert fresh.exists(), "fresh trash must NOT be reaped"
        assert len(reaped) == 1

    def test_db_vacuum_does_not_delete_file(self, kernel, owned_roots):
        """VACUUM must not delete the DB file."""
        db_path = Path(owned_roots["rawos_source_root"]) / "data" / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.commit()
        conn.close()

        kernel.vacuum_db(str(db_path))

        assert db_path.exists(), "DB file must still exist after VACUUM"


# ══════════════════════════════════════════════════════════════════════════════
# I-OWN4: Gate — operate_on_owned_resource + execute_approved_owned_op
# ══════════════════════════════════════════════════════════════════════════════

class TestOperateGate:
    @pytest.fixture(autouse=True)
    def _db(self, tmp_path):
        import rawos.db as db
        db.init(str(tmp_path / "gate_test.db"))

    def _make_stale_workspace(self, workspaces_root: str, name: str, age_days: float = 30) -> str:
        ws = Path(workspaces_root) / name
        ws.mkdir(parents=True, exist_ok=True)
        mtime = time.time() - age_days * 86400
        os.utime(ws, (mtime, mtime))
        return str(ws)

    def test_propose_when_flag_false(self, kernel, owned_roots, monkeypatch):
        from rawos.config import settings
        from rawos.kernel.owned_resource import OwnedOpSpec

        monkeypatch.setattr(settings, "operator_owned_enabled", False)
        ws = self._make_stale_workspace(owned_roots["workspaces_root"], "stale-flag")
        trash_root = str(Path(owned_roots["rawos_source_root"]) / "data" / ".trash")

        spec = OwnedOpSpec(op_type="workspace_gc", target_path=ws, trash_root=trash_root)
        outcome = kernel.operate_on_owned_resource(
            user_id="test-user", op_spec=spec, active_workspace_dirs=frozenset()
        )

        assert outcome.auto_applied is False
        assert outcome.proposed is True

    def test_propose_when_ungraduated(self, kernel, owned_roots, monkeypatch):
        from rawos.config import settings
        from rawos.kernel.owned_resource import OwnedOpSpec

        monkeypatch.setattr(settings, "operator_owned_enabled", True)
        ws = self._make_stale_workspace(owned_roots["workspaces_root"], "stale-grad")
        trash_root = str(Path(owned_roots["rawos_source_root"]) / "data" / ".trash")

        spec = OwnedOpSpec(op_type="workspace_gc", target_path=ws, trash_root=trash_root)
        outcome = kernel.operate_on_owned_resource(
            user_id="test-user", op_spec=spec, active_workspace_dirs=frozenset()
        )

        assert outcome.auto_applied is False
        assert outcome.proposed is True
        assert "graduated" in outcome.reason

    def test_auto_applies_when_enabled_and_graduated(self, kernel, owned_roots, monkeypatch):
        import hashlib
        from rawos.config import settings
        from rawos.kernel.owned_resource import OwnedOpSpec
        from rawos.models import User
        import rawos.db as db

        monkeypatch.setattr(settings, "operator_owned_enabled", True)

        # Create real user (operator_track_record has FK to users)
        user = db.create_user(User(
            email="owned-gc-test@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))

        # Seed 3 verified successes (GRADUATION_THRESHOLD=3).
        # _advance_state uses a 2-call window per verified success:
        # call A starts the window, call B confirms it → 6 calls total.
        from rawos.kernel.track_record import GRADUATION_THRESHOLD
        for i in range(GRADUATION_THRESHOLD * 2):
            db.update_operator_track_record(
                user.id, "owned_workspace_gc", owned_roots["workspaces_root"],
                verified=True, now=int(time.time()) + i,
            )

        ws = self._make_stale_workspace(owned_roots["workspaces_root"], "stale-graduated")
        trash_root = str(Path(owned_roots["rawos_source_root"]) / "data" / ".trash")

        spec = OwnedOpSpec(op_type="workspace_gc", target_path=ws, trash_root=trash_root)
        outcome = kernel.operate_on_owned_resource(
            user_id=user.id, op_spec=spec, active_workspace_dirs=frozenset()
        )

        assert outcome.auto_applied is True
        assert outcome.proposed is False
        assert not Path(ws).exists(), "workspace must be trashed"

    def test_non_owned_target_refused(self, kernel, owned_roots, monkeypatch):
        """Target outside owned namespace → OwnedResourceRefusalError (I-OWN1 unbypassable)."""
        from rawos.config import settings
        from rawos.kernel.owned_resource import OwnedOpSpec, OwnedResourceRefusalError

        monkeypatch.setattr(settings, "operator_owned_enabled", True)
        spec = OwnedOpSpec(op_type="workspace_gc", target_path="/etc")

        with pytest.raises(OwnedResourceRefusalError):
            kernel.operate_on_owned_resource(
                user_id="test-user", op_spec=spec, active_workspace_dirs=frozenset()
            )

    def test_owner_path_bypasses_flag_and_graduation(self, kernel, owned_roots, monkeypatch):
        """execute_approved_owned_op applies immediately; ignores flag + graduation."""
        from rawos.config import settings
        from rawos.kernel.owned_resource import OwnedOpSpec

        monkeypatch.setattr(settings, "operator_owned_enabled", False)  # flag OFF
        ws = self._make_stale_workspace(owned_roots["workspaces_root"], "owner-bypass")
        trash_root = str(Path(owned_roots["rawos_source_root"]) / "data" / ".trash")

        spec = OwnedOpSpec(op_type="workspace_gc", target_path=ws, trash_root=trash_root)
        result = kernel.execute_approved_owned_op(
            user_id="owner-user", op_spec=spec, active_workspace_dirs=frozenset()
        )

        assert result.applied is True
        assert not Path(ws).exists(), "workspace must be trashed despite flag=False"

    def test_owner_path_still_enforces_boundary(self, kernel, owned_roots):
        """execute_approved_owned_op must still enforce I-OWN1 boundary."""
        from rawos.kernel.owned_resource import OwnedOpSpec, OwnedResourceRefusalError

        spec = OwnedOpSpec(op_type="workspace_gc", target_path="/etc")
        with pytest.raises(OwnedResourceRefusalError):
            kernel.execute_approved_owned_op(
                user_id="owner-user", op_spec=spec, active_workspace_dirs=frozenset()
            )


# ══════════════════════════════════════════════════════════════════════════════
# I-OWN6: Audit — owned_resource_history
# ══════════════════════════════════════════════════════════════════════════════

class TestAudit:
    @pytest.fixture(autouse=True)
    def _db(self, tmp_path):
        import rawos.db as db
        db.init(str(tmp_path / "audit_test.db"))

    def test_history_records_autonomous_true(self):
        import rawos.db as db
        db.record_owned_op_outcome(
            op_type="workspace_gc",
            target_summary="/root/rawos/workspaces/abc",
            outcome="applied",
            autonomous=True,
        )
        rows = db.list_owned_resource_history()
        assert rows[0]["autonomous"] == 1

    def test_history_records_autonomous_false(self):
        import rawos.db as db
        db.record_owned_op_outcome(
            op_type="workspace_gc",
            target_summary="/root/rawos/workspaces/def",
            outcome="applied",
            autonomous=False,
        )
        rows = db.list_owned_resource_history()
        assert rows[0]["autonomous"] == 0

    def test_history_has_correct_fields(self):
        import rawos.db as db
        db.record_owned_op_outcome(
            op_type="db_vacuum",
            target_summary="rawos.db",
            outcome="applied",
        )
        rows = db.list_owned_resource_history()
        row = rows[0]
        assert row["op_type"] == "db_vacuum"
        assert row["target_summary"] == "rawos.db"
        assert row["outcome"] == "applied"
        assert "created_at" in row
