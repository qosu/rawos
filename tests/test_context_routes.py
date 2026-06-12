"""tests/test_context_routes.py — GET /context/status returns 200 for an
authenticated user.

Regression test: `context_routes.py` indexed the `current_user` dependency
result with `user["id"]`, but `current_user` returns a `User` pydantic model
(attribute access only), causing a 500 TypeError on every call.
"""
from __future__ import annotations

import os
import tempfile
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


def test_context_status_returns_200_for_authenticated_user():
    with TestClient(app) as client:
        r = client.post("/auth/signup", json={"email": "ctx@example.com", "password": "password123"})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        r = client.get("/context/status", headers=headers)

        assert r.status_code == 200
        body = r.json()
        assert body["user_id"]
        assert "intent" in body


def test_context_goals_returns_200_for_authenticated_user():
    with TestClient(app) as client:
        r = client.post("/auth/signup", json={"email": "ctx2@example.com", "password": "password123"})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        r = client.get("/context/goals", headers=headers)

        assert r.status_code == 200
        body = r.json()
        assert "proactive_artifacts" in body
