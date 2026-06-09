#!/usr/bin/env python3
"""
SQLite → PostgreSQL migration script for rawos.

Usage:
  python3 scripts/migrate_sqlite_to_postgres.py

Copies all rows from SQLite (rawos.db) to PostgreSQL (rawos database).
Idempotent: uses INSERT ... ON CONFLICT DO NOTHING.

Set RAWOS_DATABASE_URL in environment or pass --pg-url argument.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extras

SQLITE_PATH = "/root/rawos/data/rawos.db"
DEFAULT_PG_URL = "postgresql://rawos_user:rawos_pg_2026_secure@localhost/rawos"

# Tables in dependency order (parents before children)
TABLES = [
    "users",
    "projects",
    "agents",
    "intents",
    "memories",
    "artifacts",
    "events",
    "refresh_tokens",
    "billing_events",
]


def _sqlite_rows(sqlite_path: str, table: str) -> list[dict]:
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute(f"SELECT * FROM {table}")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def _get_pg_columns(pg_conn, table: str) -> list[str]:
    cur = pg_conn.cursor()
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s ORDER BY ordinal_position",
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def _migrate_table(sqlite_path: str, pg_conn, table: str) -> int:
    rows = _sqlite_rows(sqlite_path, table)
    if not rows:
        return 0
    pg_cols = _get_pg_columns(pg_conn, table)
    # Filter to columns that exist in both
    common_cols = [c for c in rows[0].keys() if c in pg_cols]
    if not common_cols:
        print(f"  WARNING: no common columns for {table}")
        return 0
    placeholders = ", ".join(["%s"] * len(common_cols))
    col_list = ", ".join(common_cols)
    sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
    cur = pg_conn.cursor()
    inserted = 0
    for row in rows:
        values = []
        for col in common_cols:
            v = row[col]
            # Convert JSON-stored payloads
            if col == "payload" and isinstance(v, str):
                try:
                    v = json.loads(v)
                    v = json.dumps(v)
                except (json.JSONDecodeError, TypeError):
                    pass
            values.append(v)
        try:
            cur.execute(sql, values)
            inserted += cur.rowcount
        except Exception as e:
            print(f"  WARN row error in {table}: {e}")
    pg_conn.commit()
    return inserted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pg-url", default=DEFAULT_PG_URL)
    parser.add_argument("--sqlite", default=SQLITE_PATH)
    args = parser.parse_args()

    if not Path(args.sqlite).exists():
        print(f"SQLite DB not found: {args.sqlite}")
        sys.exit(1)

    pg_conn = psycopg2.connect(args.pg_url)
    print(f"Connected to PostgreSQL")

    total = 0
    for table in TABLES:
        try:
            n = _migrate_table(args.sqlite, pg_conn, table)
            print(f"  {table}: {n} rows")
            total += n
        except Exception as e:
            print(f"  ERROR {table}: {e}")

    pg_conn.close()
    print(f"\nMigration complete: {total} rows total")


if __name__ == "__main__":
    main()
