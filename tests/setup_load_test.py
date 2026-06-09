#!/usr/bin/env python3
"""
Pre-create 100 load-test users directly in rawos DB.
Run ONCE before the load test. Idempotent.

    cd /root/rawos
    source venv/bin/activate
    python tests/setup_load_test.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("RAWOS_SANDBOX_DOCKER", "false")

from rawos.config import settings  # noqa: E402 — path set above

import rawos.db as db  # noqa: E402
import rawos.auth as rawos_auth  # noqa: E402
from rawos.models import Project  # noqa: E402

OUT_FILE = Path("/tmp/rawos_loadtest_users.json")
DOMAIN = "rawos.internal"
NUM_INFRA = 95
NUM_INTENT = 5
PASSWORD = "LT_Secur3_Pass!"


def _ensure_user(i: int, role: str) -> dict:
    email = f"loadtest_{i:04d}_{role}@{DOMAIN}"
    existing = db.get_user_by_email(email)
    if existing:
        _, access, _ = rawos_auth.login(email, PASSWORD)
        user = existing
    else:
        user, access, _ = rawos_auth.signup(email, PASSWORD)

    projects = db.get_projects(user.id)
    if not projects:
        p = Project(user_id=user.id, name=f"lt-project-{i:04d}")
        workdir = Path(settings.workspaces_root) / user.id / p.id
        workdir.mkdir(parents=True, exist_ok=True)
        p.workdir = str(workdir)
        db.create_project(p)
        projects = db.get_projects(user.id)

    # Ensure index.html exists so preview endpoint returns 200 during load test
    project_dir = Path(settings.workspaces_root) / user.id / projects[0].id
    index_file = project_dir / "index.html"
    if not index_file.exists():
        index_file.write_text(
            "<!DOCTYPE html><html><body><h1>rawos load test</h1></body></html>"
        )

    return {
        "email":        email,
        "role":         role,
        "access_token": access,
        "user_id":      user.id,
        "project_id":   projects[0].id,
    }


def main() -> None:
    db.init(settings.db_path)
    total = NUM_INFRA + NUM_INTENT
    pool: list[dict] = []

    print(f"Preparing {total} load-test users …")
    for i in range(total):
        role = "infra" if i < NUM_INFRA else "intent"
        slot = _ensure_user(i, role)
        pool.append(slot)
        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{total}")

    OUT_FILE.write_text(json.dumps(pool, indent=2))
    infra_count = sum(1 for s in pool if s["role"] == "infra")
    intent_count = sum(1 for s in pool if s["role"] == "intent")
    print(f"\nWrote {OUT_FILE}")
    print(f"  infra={infra_count}  intent={intent_count}")


if __name__ == "__main__":
    main()
