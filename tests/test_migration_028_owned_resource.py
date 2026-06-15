"""tests/test_migration_028_owned_resource.py — owned_resource_history table (M3)."""
from __future__ import annotations

import pytest
import rawos.db as db


@pytest.fixture()
def _db(tmp_path):
    db.init(str(tmp_path / "test.db"))


class TestOwnedResourceHistoryTable:
    def test_table_created_on_init(self, _db) -> None:
        rows = db.list_owned_resource_history()
        assert isinstance(rows, list)
        assert rows == []

    def test_record_and_retrieve(self, _db) -> None:
        db.record_owned_op_outcome(
            op_type="workspace_gc",
            target_summary="/root/rawos/workspaces/abc",
            outcome="applied",
        )
        rows = db.list_owned_resource_history()
        assert len(rows) == 1
        assert rows[0]["op_type"] == "workspace_gc"
        assert rows[0]["target_summary"] == "/root/rawos/workspaces/abc"
        assert rows[0]["outcome"] == "applied"
        assert rows[0]["autonomous"] == 0

    def test_autonomous_column_true(self, _db) -> None:
        db.record_owned_op_outcome(
            op_type="db_vacuum",
            target_summary="rawos.db",
            outcome="applied",
            autonomous=True,
        )
        rows = db.list_owned_resource_history()
        assert rows[0]["autonomous"] == 1

    def test_autonomous_defaults_false(self, _db) -> None:
        db.record_owned_op_outcome(
            op_type="workspace_gc",
            target_summary="/root/rawos/workspaces/xyz",
            outcome="proposed",
        )
        rows = db.list_owned_resource_history()
        assert rows[0]["autonomous"] == 0

    def test_multiple_rows_newest_first(self, _db) -> None:
        db.record_owned_op_outcome("workspace_gc", "ws1", "applied", autonomous=False)
        db.record_owned_op_outcome("db_vacuum", "rawos.db", "applied", autonomous=True)
        rows = db.list_owned_resource_history()
        assert rows[0]["op_type"] == "db_vacuum"
        assert rows[1]["op_type"] == "workspace_gc"

    def test_trash_ref_column_nullable(self, _db) -> None:
        db.record_owned_op_outcome(
            op_type="workspace_gc",
            target_summary="ws-no-trash",
            outcome="proposed",
            trash_ref=None,
        )
        rows = db.list_owned_resource_history()
        assert rows[0]["trash_ref"] is None

    def test_trash_ref_column_stored(self, _db) -> None:
        db.record_owned_op_outcome(
            op_type="workspace_gc",
            target_summary="ws-with-trash",
            outcome="applied",
            trash_ref="/root/rawos/data/.trash/20260615T120000_abc",
        )
        rows = db.list_owned_resource_history()
        assert rows[0]["trash_ref"] == "/root/rawos/data/.trash/20260615T120000_abc"

    def test_limit_parameter(self, _db) -> None:
        for i in range(5):
            db.record_owned_op_outcome(
                op_type="workspace_gc",
                target_summary=f"ws{i}",
                outcome="applied",
            )
        rows = db.list_owned_resource_history(limit=3)
        assert len(rows) == 3
