"""
Tests for migration 029 — venv_operator_history table.
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

import rawos.db as db


def _fresh_db(tmp_path: Path) -> sqlite3.Connection:
    dbfile = tmp_path / "test.db"
    conn = sqlite3.connect(str(dbfile))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _apply_schema(tmp_path: Path) -> None:
    db_path = str(tmp_path / "rawos.db")
    db.init(db_path)


class TestMigration029VenvOperatorHistory:
    def test_table_created(self, tmp_path: Path) -> None:
        _apply_schema(tmp_path)
        with db._conn() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='venv_operator_history'"
            ).fetchone()
        assert row is not None, "venv_operator_history table must be created by migration 029"

    def test_record_and_retrieve(self, tmp_path: Path) -> None:
        _apply_schema(tmp_path)
        with db._conn() as conn:
            conn.execute(
                """INSERT INTO venv_operator_history
                   (op_type, frozen_hash_before, frozen_hash_after, outcome)
                   VALUES (?, ?, ?, ?)""",
                ("dep_update", "abc", "def", "applied"),
            )
            row = conn.execute(
                "SELECT * FROM venv_operator_history"
            ).fetchone()
        assert row["op_type"] == "dep_update"
        assert row["frozen_hash_before"] == "abc"
        assert row["frozen_hash_after"] == "def"
        assert row["outcome"] == "applied"
        assert row["autonomous"] == 0

    def test_autonomous_column_default_zero(self, tmp_path: Path) -> None:
        _apply_schema(tmp_path)
        with db._conn() as conn:
            conn.execute(
                """INSERT INTO venv_operator_history
                   (op_type, frozen_hash_before, frozen_hash_after, outcome)
                   VALUES ('dep_update', 'a', 'b', 'proposed')"""
            )
            row = conn.execute("SELECT autonomous FROM venv_operator_history").fetchone()
        assert row["autonomous"] == 0

    def test_autonomous_column_stores_true(self, tmp_path: Path) -> None:
        _apply_schema(tmp_path)
        with db._conn() as conn:
            conn.execute(
                """INSERT INTO venv_operator_history
                   (op_type, frozen_hash_before, frozen_hash_after, outcome, autonomous)
                   VALUES ('dep_update', 'x', 'y', 'applied', 1)"""
            )
            row = conn.execute("SELECT autonomous FROM venv_operator_history").fetchone()
        assert row["autonomous"] == 1

    def test_outcome_check_constraint(self, tmp_path: Path) -> None:
        _apply_schema(tmp_path)
        with pytest.raises(sqlite3.IntegrityError):
            with db._conn() as conn:
                conn.execute(
                    """INSERT INTO venv_operator_history
                       (op_type, frozen_hash_before, frozen_hash_after, outcome)
                       VALUES ('dep_update', 'a', 'b', 'not_a_valid_outcome')"""
                )

    def test_multiple_rows_newest_first_by_rowid(self, tmp_path: Path) -> None:
        _apply_schema(tmp_path)
        with db._conn() as conn:
            conn.execute(
                """INSERT INTO venv_operator_history
                   (op_type, frozen_hash_before, frozen_hash_after, outcome)
                   VALUES ('dep_update', 'first', 'first2', 'proposed')"""
            )
            conn.execute(
                """INSERT INTO venv_operator_history
                   (op_type, frozen_hash_before, frozen_hash_after, outcome)
                   VALUES ('dep_update', 'second', 'second2', 'applied')"""
            )
            rows = conn.execute(
                "SELECT frozen_hash_before FROM venv_operator_history ORDER BY rowid DESC"
            ).fetchall()
        assert rows[0]["frozen_hash_before"] == "second"
        assert rows[1]["frozen_hash_before"] == "first"

    def test_id_primary_key_auto(self, tmp_path: Path) -> None:
        _apply_schema(tmp_path)
        with db._conn() as conn:
            conn.execute(
                """INSERT INTO venv_operator_history
                   (op_type, frozen_hash_before, frozen_hash_after, outcome)
                   VALUES ('dep_update', 'a', 'b', 'applied')"""
            )
            row = conn.execute("SELECT id FROM venv_operator_history").fetchone()
        assert row["id"] is not None
        assert len(row["id"]) == 32  # lower(hex(randomblob(16)))

    def test_created_at_defaults_to_unixepoch(self, tmp_path: Path) -> None:
        import time as _time
        _apply_schema(tmp_path)
        before = int(_time.time())
        with db._conn() as conn:
            conn.execute(
                """INSERT INTO venv_operator_history
                   (op_type, frozen_hash_before, frozen_hash_after, outcome)
                   VALUES ('dep_update', 'a', 'b', 'liveness_failed')"""
            )
            row = conn.execute("SELECT created_at FROM venv_operator_history").fetchone()
        after = int(_time.time())
        assert before <= row["created_at"] <= after
