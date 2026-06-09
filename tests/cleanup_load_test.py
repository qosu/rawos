#!/usr/bin/env python3
"""
Delete all load-test users and their data from rawos DB + workspaces.
Run after every load-test session to keep SQLite clean.

    cd /root/rawos
    source venv/bin/activate
    python tests/cleanup_load_test.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("RAWOS_SANDBOX_DOCKER", "false")

from rawos.config import settings  # noqa: E402
import rawos.db as db  # noqa: E402

DOMAIN = "rawos.internal"
POOL_FILE = Path("/tmp/rawos_loadtest_users.json")


def main() -> None:
    db.init(settings.db_path)

    db_path = str(settings.db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute(
            f"SELECT id FROM users WHERE email LIKE '%@{DOMAIN}'"
        ).fetchall()
        user_ids = [r["id"] for r in rows]

        if not user_ids:
            print("No load-test users found — nothing to clean.")
            return

        print(f"Cleaning {len(user_ids)} load-test users …")
        placeholders = ",".join("?" * len(user_ids))

        # Delete in FK dependency order
        for table in (
            "memories", "intents", "artifacts",
            "agents", "billing_events", "projects", "users",
        ):
            try:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE user_id IN ({placeholders})",
                    user_ids,
                )
                if cur.rowcount:
                    print(f"  {table}: deleted {cur.rowcount} rows")
            except sqlite3.OperationalError:
                pass  # table may not exist

        conn.commit()

        # Remove workspace directories
        workspaces_root = Path(settings.workspaces_root)
        removed_dirs = 0
        for uid in user_ids:
            user_dir = workspaces_root / uid
            if user_dir.exists():
                shutil.rmtree(user_dir)
                removed_dirs += 1
        if removed_dirs:
            print(f"  workspaces: removed {removed_dirs} directories")

        print(f"\nCleanup complete — {len(user_ids)} users removed.")

    finally:
        conn.close()

    # Remove pool file
    if POOL_FILE.exists():
        POOL_FILE.unlink()
        print(f"Deleted {POOL_FILE}")


if __name__ == "__main__":
    main()
