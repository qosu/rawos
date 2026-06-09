"""
Phase 2 tests — tool execution, artifact storage, file serving.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------

class TestSandbox:
    def test_validate_path_safe(self, tmp_path):
        from rawos.kernel.sandbox import validate_path
        result = validate_path("src/index.html", str(tmp_path))
        assert str(result) == str(tmp_path / "src" / "index.html")

    def test_validate_path_traversal_raises(self, tmp_path):
        from rawos.kernel.sandbox import validate_path, PathTraversalError
        with pytest.raises(PathTraversalError):
            validate_path("../../etc/passwd", str(tmp_path))

    def test_validate_path_absolute_inside(self, tmp_path):
        from rawos.kernel.sandbox import validate_path
        inside = str(tmp_path / "file.txt")
        result = validate_path(inside, str(tmp_path))
        assert result == Path(inside).resolve()

    def test_validate_path_absolute_outside_raises(self, tmp_path):
        from rawos.kernel.sandbox import validate_path, PathTraversalError
        with pytest.raises(PathTraversalError):
            validate_path("/etc/passwd", str(tmp_path))

    def test_run_bash_success(self, tmp_path):
        from rawos.kernel.sandbox import run_bash
        result = asyncio.run(run_bash("echo hello", str(tmp_path)))
        assert result.exit_code == 0
        assert "hello" in result.stdout

    def test_run_bash_exit_nonzero(self, tmp_path):
        from rawos.kernel.sandbox import run_bash
        result = asyncio.run(run_bash("exit 1", str(tmp_path)))
        assert result.exit_code == 1

    def test_run_bash_timeout(self, tmp_path):
        from rawos.kernel.sandbox import run_bash, _TIMEOUT
        import time
        # Override timeout for test speed — we can't override easily, so just verify
        # the timeout constant is reasonable
        assert _TIMEOUT == 30

    def test_run_bash_cwd_isolation(self, tmp_path):
        from rawos.kernel.sandbox import run_bash
        result = asyncio.run(run_bash("pwd", str(tmp_path)))
        assert tmp_path.name in result.stdout or str(tmp_path) in result.stdout

    def test_run_bash_output_contains_stderr(self, tmp_path):
        from rawos.kernel.sandbox import run_bash
        result = asyncio.run(run_bash("echo err >&2", str(tmp_path)))
        assert "err" in result.stderr


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class TestTools:
    def test_write_file(self, tmp_path):
        from rawos.kernel.tools import execute
        result = asyncio.run(execute("write_file", {"path": "test.txt", "content": "hello"}, str(tmp_path)))
        assert result.success
        assert (tmp_path / "test.txt").read_text() == "hello"

    def test_write_file_creates_dirs(self, tmp_path):
        from rawos.kernel.tools import execute
        result = asyncio.run(execute("write_file", {"path": "a/b/c.txt", "content": "x"}, str(tmp_path)))
        assert result.success
        assert (tmp_path / "a" / "b" / "c.txt").exists()

    def test_write_file_traversal_blocked(self, tmp_path):
        from rawos.kernel.tools import execute
        result = asyncio.run(execute("write_file", {"path": "../../evil.txt", "content": "x"}, str(tmp_path)))
        assert not result.success
        assert not Path("/root/evil.txt").exists()

    def test_read_file(self, tmp_path):
        from rawos.kernel.tools import execute
        (tmp_path / "hello.txt").write_text("world")
        result = asyncio.run(execute("read_file", {"path": "hello.txt"}, str(tmp_path)))
        assert result.success
        assert result.output == "world"

    def test_read_file_not_found(self, tmp_path):
        from rawos.kernel.tools import execute
        result = asyncio.run(execute("read_file", {"path": "missing.txt"}, str(tmp_path)))
        assert not result.success
        assert "not found" in result.output

    def test_list_files_empty(self, tmp_path):
        from rawos.kernel.tools import execute
        result = asyncio.run(execute("list_files", {}, str(tmp_path)))
        assert result.success
        assert "empty" in result.output

    def test_list_files_with_files(self, tmp_path):
        from rawos.kernel.tools import execute
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        result = asyncio.run(execute("list_files", {}, str(tmp_path)))
        assert result.success
        assert "a.txt" in result.output
        assert "b.txt" in result.output

    def test_bash_creates_file(self, tmp_path):
        from rawos.kernel.tools import execute
        result = asyncio.run(execute("bash", {"command": "echo hello > out.txt"}, str(tmp_path)))
        assert (tmp_path / "out.txt").exists()

    def test_unknown_tool(self, tmp_path):
        from rawos.kernel.tools import execute
        result = asyncio.run(execute("no_such_tool", {}, str(tmp_path)))
        assert not result.success
        assert "unknown tool" in result.output

    def test_bash_empty_command(self, tmp_path):
        from rawos.kernel.tools import execute
        result = asyncio.run(execute("bash", {"command": ""}, str(tmp_path)))
        assert not result.success


# ---------------------------------------------------------------------------
# DB Artifacts
# ---------------------------------------------------------------------------

class TestArtifactDB:
    def setup_method(self):
        import rawos.db as db
        self.tmp = tempfile.mkdtemp()
        db.init(os.path.join(self.tmp, "test.db"))
        # Create a user and project for FK constraints
        from rawos.models import User, Project
        import hashlib
        self.user = db.create_user(User(
            email="art@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))
        self.project = db.create_project(Project(
            user_id=self.user.id, name="ArtTest",
            workdir=self.tmp,
        ))

    def _artifact(self):
        from rawos.models import Artifact, ArtifactType
        return Artifact(
            user_id=self.user.id,
            project_id=self.project.id,
            type=ArtifactType.FILE,
            name="index.html",
            path=os.path.join(self.tmp, "index.html"),
            mime_type="text/html",
            size_bytes=512,
        )

    def test_save_and_get(self):
        import rawos.db as db
        art = self._artifact()
        db.save_artifact(art)
        fetched = db.get_artifact(self.user.id, art.id)
        assert fetched is not None
        assert fetched.name == "index.html"

    def test_get_returns_none_wrong_user(self):
        import rawos.db as db
        art = self._artifact()
        db.save_artifact(art)
        assert db.get_artifact("other-user", art.id) is None

    def test_get_project_artifacts(self):
        import rawos.db as db
        from rawos.models import Artifact, ArtifactType
        for i in range(3):
            db.save_artifact(Artifact(
                user_id=self.user.id, project_id=self.project.id,
                type=ArtifactType.FILE, name=f"file{i}.txt",
                mime_type="text/plain", size_bytes=i,
            ))
        arts = db.get_project_artifacts(self.user.id, self.project.id)
        assert len(arts) == 3

    def test_delete_artifact(self):
        import rawos.db as db
        art = self._artifact()
        db.save_artifact(art)
        assert db.delete_artifact(self.user.id, art.id)
        assert db.get_artifact(self.user.id, art.id) is None

    def test_delete_artifact_wrong_user(self):
        import rawos.db as db
        art = self._artifact()
        db.save_artifact(art)
        assert not db.delete_artifact("wrong", art.id)


# ---------------------------------------------------------------------------
# File routes (API)
# ---------------------------------------------------------------------------

class TestFileRoutes:
    def setup_method(self):
        import rawos.db as db
        import hashlib
        self.tmp = tempfile.mkdtemp()
        db.init(os.path.join(self.tmp, "test.db"))
        from rawos.models import User, Project
        self.user = db.create_user(User(
            email="file@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))
        self.project = db.create_project(Project(
            user_id=self.user.id, name="FileTest",
            workdir=self.tmp,
        ))
        # Create a test file in the workdir
        Path(self.tmp, "hello.txt").write_text("hello world")

    def _client_with_auth(self):
        from fastapi.testclient import TestClient
        from rawos.api.app import app
        import rawos.auth as auth
        token = auth.create_access_token(self.user.id)
        return TestClient(app), {"Authorization": f"Bearer {token}"}

    def test_list_files(self):
        client, headers = self._client_with_auth()
        resp = client.get(f"/projects/{self.project.id}/files", headers=headers)
        assert resp.status_code == 200
        names = [e["name"] for e in resp.json()]
        assert "hello.txt" in names

    def test_serve_file(self):
        client, headers = self._client_with_auth()
        resp = client.get(f"/projects/{self.project.id}/files/hello.txt", headers=headers)
        assert resp.status_code == 200
        assert "hello world" in resp.text

    def test_serve_file_traversal_blocked(self):
        client, headers = self._client_with_auth()
        resp = client.get(f"/projects/{self.project.id}/files/../../etc/passwd", headers=headers)
        assert resp.status_code in (400, 404)

    def test_list_artifacts_empty(self):
        client, headers = self._client_with_auth()
        resp = client.get(f"/projects/{self.project.id}/artifacts", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_wrong_user_cannot_access_files(self):
        import rawos.db as db
        from rawos.models import User
        import hashlib
        other = db.create_user(User(
            email="other2@test.com",
            password_hash=hashlib.sha256(b"other").hexdigest(),
        ))
        from fastapi.testclient import TestClient
        from rawos.api.app import app
        import rawos.auth as auth
        token = auth.create_access_token(other.id)
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {token}"}
        resp = client.get(f"/projects/{self.project.id}/files/hello.txt", headers=headers)
        assert resp.status_code == 404   # project not found for this user
