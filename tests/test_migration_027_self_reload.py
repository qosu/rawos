"""tests/test_migration_027_self_reload.py — autonomous column on managed_self_reload (Phase 25 Stage 2)."""
from __future__ import annotations

import pytest

import rawos.db as db


@pytest.fixture()
def _db(tmp_path):
    db.init(str(tmp_path / "test.db"))


class TestManagedSelfReloadAutonomousColumn:
    def test_autonomous_column_defaults_zero(self, _db) -> None:
        db.record_self_reload_outcome("OLDSHA", "NEWSHA", "committed")
        rows = db.list_self_reload_history()
        assert rows[0]["autonomous"] == 0

    def test_record_with_autonomous_true(self, _db) -> None:
        db.record_self_reload_outcome("OLDSHA", "NEWSHA", "committed", autonomous=True)
        rows = db.list_self_reload_history()
        assert rows[0]["autonomous"] == 1

    def test_record_with_autonomous_false_explicit(self, _db) -> None:
        db.record_self_reload_outcome("OLDSHA", "NEWSHA", "resurrected", autonomous=False)
        rows = db.list_self_reload_history()
        assert rows[0]["autonomous"] == 0

    def test_autonomous_is_per_row(self, _db) -> None:
        db.record_self_reload_outcome("SHA0", "SHA1", "committed", autonomous=False)
        db.record_self_reload_outcome("SHA1", "SHA2", "committed", autonomous=True)
        rows = db.list_self_reload_history()
        # Newest first
        assert rows[0]["autonomous"] == 1
        assert rows[1]["autonomous"] == 0
