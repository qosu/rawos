"""tests/test_pam_targets.py — TDD for managed_pam_targets DB accessors (Phase 22)."""
from __future__ import annotations

import hashlib
import os
import tempfile

import pytest

import rawos.db as db
from rawos.models import User


@pytest.fixture()
def user_id(tmp_path):
    db.init(str(tmp_path / "test.db"))
    user = db.create_user(User(
        email=f"pam-test-{os.getpid()}@test.com",
        password_hash=hashlib.sha256(b"pw").hexdigest(),
    ))
    return user.id


class TestManagedPamTargets:
    def test_get_nonexistent_returns_none(self, user_id: str) -> None:
        assert db.get_managed_pam_target(user_id, "rawos-guest") is None

    def test_add_then_get(self, user_id: str) -> None:
        db.add_managed_pam_target(user_id, "rawos-guest")
        row = db.get_managed_pam_target(user_id, "rawos-guest")
        assert row is not None
        assert row["pam_file"] == "rawos-guest"

    def test_add_is_idempotent(self, user_id: str) -> None:
        db.add_managed_pam_target(user_id, "rawos-guest")
        db.add_managed_pam_target(user_id, "rawos-guest")  # no-op, no exception
        assert db.get_managed_pam_target(user_id, "rawos-guest") is not None

    def test_list_returns_all(self, user_id: str) -> None:
        db.add_managed_pam_target(user_id, "rawos-guest")
        db.add_managed_pam_target(user_id, "rawos-tenant")
        rows = db.list_managed_pam_targets(user_id)
        pam_files = {r["pam_file"] for r in rows}
        assert pam_files == {"rawos-guest", "rawos-tenant"}

    def test_list_empty_for_new_user(self, user_id: str) -> None:
        assert db.list_managed_pam_targets(user_id) == []

    def test_remove_deregisters(self, user_id: str) -> None:
        db.add_managed_pam_target(user_id, "rawos-guest")
        db.remove_managed_pam_target(user_id, "rawos-guest")
        assert db.get_managed_pam_target(user_id, "rawos-guest") is None

    def test_remove_noop_if_absent(self, user_id: str) -> None:
        db.remove_managed_pam_target(user_id, "rawos-ghost")  # no exception

    def test_targets_are_per_user(self, tmp_path) -> None:
        db.init(str(tmp_path / "test.db"))
        u1 = db.create_user(User(
            email=f"u1-{os.getpid()}@test.com",
            password_hash=hashlib.sha256(b"pw").hexdigest(),
        ))
        u2 = db.create_user(User(
            email=f"u2-{os.getpid()}@test.com",
            password_hash=hashlib.sha256(b"pw").hexdigest(),
        ))
        db.add_managed_pam_target(u1.id, "rawos-guest")
        assert db.get_managed_pam_target(u2.id, "rawos-guest") is None
        assert db.list_managed_pam_targets(u2.id) == []
