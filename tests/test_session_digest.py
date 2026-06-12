"""tests/test_session_digest.py — TDD for "while you were away" digest.

Covers:
  DB layer: get_last_chat_at, set_last_chat_at, get_proactive_artifacts_since
  API layer: POST /context/session_start
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")
os.environ["WORKSPACES_ROOT"] = str(Path(tempfile.mkdtemp()))
os.environ["JWT_SECRET"] = "test_secret_32chars_minimum_ok"
os.environ["DEEPSEEK_KEY"] = "test_key"

from rawos.api.app import app
from rawos.config import settings
import rawos.db as db
from rawos.auth import hash_password
from rawos.models import User


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    ws_root = str(tmp_path / "ws")
    os.environ["DB_PATH"] = db_path
    os.environ["WORKSPACES_ROOT"] = ws_root
    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(settings, "workspaces_root", ws_root)
    db.init(db_path)
    yield


def _make_user(email: str = "digest@example.com") -> User:
    return db.create_user(User(email=email, password_hash=hash_password("password123")))


def _insert_proactive_artifact(user_id: str, goal: str, created_at: int) -> None:
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO proactive_artifacts (user_id, goal, confidence, file_path, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, goal, 0.9, "/tmp/artifact.py", created_at),
        )


# ── DB-layer ──────────────────────────────────────────────────────────────────

def test_get_last_chat_at_returns_zero_for_unknown_user():
    result = db.get_last_chat_at("nonexistent-user-id")
    assert result == 0


def test_set_and_get_last_chat_at_roundtrip():
    user = _make_user()
    db.set_last_chat_at(user.id, 1_700_000_000)
    assert db.get_last_chat_at(user.id) == 1_700_000_000


def test_set_last_chat_at_overwrites_previous():
    user = _make_user("overwrite@example.com")
    db.set_last_chat_at(user.id, 1_000_000)
    db.set_last_chat_at(user.id, 2_000_000)
    assert db.get_last_chat_at(user.id) == 2_000_000


def test_get_proactive_artifacts_since_returns_empty_when_none():
    user = _make_user("empty@example.com")
    result = db.get_proactive_artifacts_since(user.id, since_ts=0)
    assert result == []


def test_get_proactive_artifacts_since_filters_by_timestamp():
    user = _make_user("filter@example.com")
    _insert_proactive_artifact(user.id, "goal-old", created_at=100)
    _insert_proactive_artifact(user.id, "goal-mid", created_at=200)
    _insert_proactive_artifact(user.id, "goal-new", created_at=300)

    result = db.get_proactive_artifacts_since(user.id, since_ts=150)

    goals = {r["goal"] for r in result}
    assert goals == {"goal-mid", "goal-new"}
    assert len(result) == 2


# ── API-layer ─────────────────────────────────────────────────────────────────

def _signup_and_headers(client, email: str) -> tuple[dict, str]:
    r = client.post("/auth/signup", json={"email": email, "password": "password123"})
    assert r.status_code == 201
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    r = client.get("/auth/me", headers=headers)
    assert r.status_code == 200
    user_id = r.json()["id"]
    return headers, user_id


def test_session_start_returns_empty_on_fresh_user():
    with TestClient(app) as client:
        headers, _ = _signup_and_headers(client, "fresh@example.com")

        r = client.post("/context/session_start", headers=headers)

        assert r.status_code == 200
        body = r.json()
        assert body["last_chat_at"] == 0
        assert body["artifacts"] == []


def test_session_start_returns_artifacts_since_last_chat():
    with TestClient(app) as client:
        headers, user_id = _signup_and_headers(client, "artifacts@example.com")

        db.set_last_chat_at(user_id, 500)
        _insert_proactive_artifact(user_id, "fix-auth-bug", created_at=600)
        _insert_proactive_artifact(user_id, "refactor-old", created_at=400)

        r = client.post("/context/session_start", headers=headers)

        assert r.status_code == 200
        body = r.json()
        assert len(body["artifacts"]) == 1
        assert body["artifacts"][0]["goal"] == "fix-auth-bug"


def test_session_start_updates_last_chat_at():
    with TestClient(app) as client:
        headers, user_id = _signup_and_headers(client, "update@example.com")

        before = int(time.time()) - 1
        r = client.post("/context/session_start", headers=headers)
        after = int(time.time()) + 1

        assert r.status_code == 200
        new_ts = db.get_last_chat_at(user_id)
        assert before <= new_ts <= after
