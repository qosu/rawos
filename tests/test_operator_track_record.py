"""tests/test_operator_track_record.py — TDD for operator_track_record DB + graduation.

Uses the same class-based setup pattern as test_track_record_io.py.
_advance_state() is reused verbatim — these tests verify:
  (1) fresh state for unknown keys,
  (2) stability window starts on first success,
  (3) two consecutive successes complete one verified_success,
  (4) failure resets the pending window,
  (5) class graduates after GRADUATION_THRESHOLD verified successes,
  (6) PK scoping: distinct (operation_class, target) keys are independent,
  (7) autonomy_track_record rows are never touched (no shared-state regression).
"""
from __future__ import annotations

import hashlib
import os
import tempfile

import rawos.db as db
from rawos.kernel.track_record import GRADUATION_THRESHOLD
from rawos.models import User


class TestOperatorTrackRecordDB:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        db.init(os.path.join(self.tmp, "test.db"))
        self.user = db.create_user(User(
            email=f"op-track-{id(self)}@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))

    def test_get_returns_fresh_state_for_unknown_key(self):
        state = db.get_operator_track_record(
            self.user.id, "file_edit", "/etc/some-target.conf"
        )
        assert state.verified_successes == 0
        assert state.graduated is False
        assert state.pending_since is None
        assert state.last_outcome is None

    def test_first_success_starts_stability_window(self):
        state = db.update_operator_track_record(
            self.user.id, "file_edit", "/etc/target.conf",
            verified=True, now=1000,
        )
        assert state.last_outcome == "merged_pending_stability"
        assert state.pending_since == 1000
        assert state.verified_successes == 0

    def test_two_consecutive_successes_complete_one_verified(self):
        db.update_operator_track_record(
            self.user.id, "file_edit", "/etc/target.conf",
            verified=True, now=1000,
        )
        state = db.update_operator_track_record(
            self.user.id, "file_edit", "/etc/target.conf",
            verified=True, now=2000,
        )
        assert state.verified_successes == 1
        assert state.last_outcome == "merged_resolved"
        assert state.pending_since is None

    def test_failure_resets_pending_window(self):
        db.update_operator_track_record(
            self.user.id, "file_edit", "/etc/target.conf",
            verified=True, now=1000,
        )
        state = db.update_operator_track_record(
            self.user.id, "file_edit", "/etc/target.conf",
            verified=False, now=2000,
        )
        assert state.last_outcome == "merged_regressed"
        assert state.pending_since is None
        assert state.verified_successes == 0

    def test_class_graduates_after_graduation_threshold_verified_successes(self):
        assert GRADUATION_THRESHOLD == 3
        for i in range(GRADUATION_THRESHOLD):
            db.update_operator_track_record(
                self.user.id, "file_edit", "/etc/target.conf",
                verified=True, now=i * 1000,
            )
            db.update_operator_track_record(
                self.user.id, "file_edit", "/etc/target.conf",
                verified=True, now=i * 1000 + 500,
            )
        state = db.get_operator_track_record(
            self.user.id, "file_edit", "/etc/target.conf"
        )
        assert state.verified_successes == GRADUATION_THRESHOLD
        assert state.graduated is True

    def test_distinct_targets_are_independent(self):
        db.update_operator_track_record(
            self.user.id, "file_edit", "/etc/a.conf",
            verified=True, now=1000,
        )
        db.update_operator_track_record(
            self.user.id, "file_edit", "/etc/a.conf",
            verified=True, now=2000,
        )
        state_b = db.get_operator_track_record(
            self.user.id, "file_edit", "/etc/b.conf"
        )
        assert state_b.verified_successes == 0
        assert state_b.pending_since is None

    def test_autonomy_track_record_unaffected_by_operator_ops(self):
        db.update_operator_track_record(
            self.user.id, "file_edit", "/etc/target.conf",
            verified=True, now=1000,
        )
        from rawos.kernel.track_record import get_track_record
        state = get_track_record(
            self.user.id, "/root/some-repo", "service_failed:foo.service"
        )
        assert state.verified_successes == 0
        assert state.graduated is False
        assert state.last_outcome is None
