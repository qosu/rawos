"""tests/test_operator_service_allowlist.py — TDD for managed_service_targets DB
accessors + operator_track_record graduation on service_* classes (Phase 23a, Step 3).

Mirrors tests/test_operator_allowlist.py for the file-target accessors.
"""
from __future__ import annotations

import hashlib
import os
import tempfile

import rawos.db as db
from rawos.kernel.track_record import GRADUATION_THRESHOLD
from rawos.models import User


class TestManagedServiceTargetsDB:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        db.init(os.path.join(self.tmp, "test.db"))
        self.user = db.create_user(User(
            email=f"svc-allowlist-{id(self)}@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))

    def test_get_returns_none_for_unregistered_target(self):
        result = db.get_managed_service_target(self.user.id, "rawos-svcprobe.service")
        assert result is None

    def test_add_then_get_roundtrip(self):
        db.add_managed_service_target(
            self.user.id, "rawos-svcprobe.service", "systemctl is-active --quiet rawos-svcprobe"
        )
        row = db.get_managed_service_target(self.user.id, "rawos-svcprobe.service")
        assert row is not None
        assert row["service_name"] == "rawos-svcprobe.service"
        assert row["validator_cmd"] == "systemctl is-active --quiet rawos-svcprobe"

    def test_add_upserts_validator_cmd(self):
        db.add_managed_service_target(self.user.id, "probe.service", "validate-v1")
        db.add_managed_service_target(self.user.id, "probe.service", "validate-v2")
        row = db.get_managed_service_target(self.user.id, "probe.service")
        assert row["validator_cmd"] == "validate-v2"

    def test_remove_makes_target_absent(self):
        db.add_managed_service_target(self.user.id, "probe.service", "true")
        db.remove_managed_service_target(self.user.id, "probe.service")
        assert db.get_managed_service_target(self.user.id, "probe.service") is None

    def test_remove_noop_for_unregistered_target(self):
        db.remove_managed_service_target(self.user.id, "never-added.service")

    def test_targets_scoped_to_user_id(self):
        other_user = db.create_user(User(
            email=f"svc-other-{id(self)}@test.com",
            password_hash=hashlib.sha256(b"pass2").hexdigest(),
        ))
        db.add_managed_service_target(self.user.id, "probe.service", "true")
        assert db.get_managed_service_target(other_user.id, "probe.service") is None

    def test_list_returns_empty_for_user_with_no_targets(self):
        assert db.list_managed_service_targets(self.user.id) == []

    def test_list_returns_only_this_user_rows(self):
        other_user = db.create_user(User(
            email=f"svc-lister-{id(self)}@test.com",
            password_hash=hashlib.sha256(b"pass3").hexdigest(),
        ))
        db.add_managed_service_target(self.user.id, "a.service", "validate-a")
        db.add_managed_service_target(self.user.id, "b.service", "validate-b")
        db.add_managed_service_target(other_user.id, "c.service", "validate-c")

        rows = db.list_managed_service_targets(self.user.id)

        assert {r["service_name"] for r in rows} == {"a.service", "b.service"}
        assert {r["validator_cmd"] for r in rows} == {"validate-a", "validate-b"}


class TestServiceTrackRecordGraduation:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        db.init(os.path.join(self.tmp, "test.db"))
        self.user = db.create_user(User(
            email=f"svc-grad-{id(self)}@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))

    def test_service_restart_graduates_after_threshold_successes(self):
        target = "rawos-svcprobe.service"
        operation_class = "service_restart"
        now = 1_700_000_000
        # _advance_state requires a 2-cycle stability window per success;
        # GRADUATION_THRESHOLD=3 successes → 3*2=6 calls to graduate.
        for i in range(GRADUATION_THRESHOLD * 2):
            db.update_operator_track_record(
                self.user.id, operation_class, target, verified=True, now=now + i,
            )

        track = db.get_operator_track_record(self.user.id, operation_class, target)
        assert track.graduated is True

    def test_service_track_record_independent_of_file_edit(self):
        target = "rawos-svcprobe.service"
        now = 1_700_000_000

        for i in range(GRADUATION_THRESHOLD * 2):
            db.update_operator_track_record(
                self.user.id, "service_restart", target, verified=True, now=now + i,
            )

        file_track = db.get_operator_track_record(self.user.id, "file_edit", target)
        assert file_track.graduated is False
