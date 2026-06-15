"""Auth + full API integration tests."""
import pytest
from fastapi.testclient import TestClient
from pathlib import Path
import tempfile
import os

# Point to temp DB before importing app
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")
os.environ["WORKSPACES_ROOT"] = str(Path(tempfile.mkdtemp()))
os.environ["JWT_SECRET"] = "test_secret_32chars_minimum_ok"
os.environ["DEEPSEEK_KEY"] = "test_key"

from rawos.api.app import app
import rawos.db as db

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    os.environ["DB_PATH"] = str(tmp_path / "test.db")
    os.environ["WORKSPACES_ROOT"] = str(tmp_path / "ws")
    db.init(os.environ["DB_PATH"])
    yield


class TestHealth:
    @pytest.mark.self_reload_smoke
    def test_health_ok(self):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestSignup:
    def test_signup_ok(self):
        r = client.post("/auth/signup", json={"email": "user@test.com", "password": "password123"})
        assert r.status_code == 201
        data = r.json()
        assert "access_token" in data
        assert "refresh_token" in data

    def test_signup_duplicate_email(self):
        client.post("/auth/signup", json={"email": "dup@test.com", "password": "password123"})
        r = client.post("/auth/signup", json={"email": "dup@test.com", "password": "password123"})
        assert r.status_code == 400
        assert "already registered" in r.json()["detail"]

    def test_signup_short_password(self):
        r = client.post("/auth/signup", json={"email": "x@test.com", "password": "short"})
        assert r.status_code == 400

    def test_signup_invalid_email(self):
        r = client.post("/auth/signup", json={"email": "notanemail", "password": "password123"})
        assert r.status_code in (400, 422)


class TestLogin:
    def test_login_ok(self):
        client.post("/auth/signup", json={"email": "login@test.com", "password": "password123"})
        r = client.post("/auth/login", json={"email": "login@test.com", "password": "password123"})
        assert r.status_code == 200
        assert "access_token" in r.json()

    def test_login_wrong_password(self):
        client.post("/auth/signup", json={"email": "wrong@test.com", "password": "password123"})
        r = client.post("/auth/login", json={"email": "wrong@test.com", "password": "wrongpass"})
        assert r.status_code == 401

    def test_login_unknown_email(self):
        r = client.post("/auth/login", json={"email": "ghost@test.com", "password": "password123"})
        assert r.status_code == 401


class TestMe:
    def test_me_authenticated(self):
        r = client.post("/auth/signup", json={"email": "me@test.com", "password": "password123"})
        token = r.json()["access_token"]
        r2 = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r2.status_code == 200
        data = r2.json()
        assert data["email"] == "me@test.com"
        assert "password_hash" not in data

    def test_me_unauthenticated(self):
        r = client.get("/auth/me")
        assert r.status_code == 401

    def test_me_invalid_token(self):
        r = client.get("/auth/me", headers={"Authorization": "Bearer invalidtoken"})
        assert r.status_code == 401


class TestRefresh:
    def test_refresh_ok(self):
        r = client.post("/auth/signup", json={"email": "ref@test.com", "password": "password123"})
        refresh = r.json()["refresh_token"]
        r2 = client.post("/auth/refresh", json={"refresh_token": refresh})
        assert r2.status_code == 200
        assert "access_token" in r2.json()

    def test_refresh_invalid(self):
        r = client.post("/auth/refresh", json={"refresh_token": "badtoken"})
        assert r.status_code == 401

    def test_refresh_single_use(self):
        r = client.post("/auth/signup", json={"email": "su@test.com", "password": "password123"})
        refresh = r.json()["refresh_token"]
        client.post("/auth/refresh", json={"refresh_token": refresh})
        r2 = client.post("/auth/refresh", json={"refresh_token": refresh})
        assert r2.status_code == 401


class TestProjects:
    def _auth_header(self, email="proj@test.com"):
        r = client.post("/auth/signup", json={"email": email, "password": "password123"})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def test_create_project(self):
        headers = self._auth_header()
        r = client.post("/projects", json={"name": "My App"}, headers=headers)
        assert r.status_code == 201
        data = r.json()
        assert data["name"] == "My App"
        assert data["workdir"] != ""

    def test_list_projects(self):
        headers = self._auth_header("list@test.com")
        client.post("/projects", json={"name": "P1"}, headers=headers)
        client.post("/projects", json={"name": "P2"}, headers=headers)
        r = client.get("/projects", headers=headers)
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_project_isolation(self):
        h1 = self._auth_header("u1@test.com")
        h2 = self._auth_header("u2@test.com")
        r = client.post("/projects", json={"name": "Secret"}, headers=h1)
        pid = r.json()["id"]
        r2 = client.get(f"/projects/{pid}", headers=h2)
        assert r2.status_code == 404

    def test_unauthenticated_blocked(self):
        r = client.post("/projects", json={"name": "X"})
        assert r.status_code == 401



class TestInternalSelfReload:
    """Phase 25 Stage 1c -- /internal/self-reload/arm-and-go, loopback-only.

    Runs execute_owner_self_reload() IN-PROCESS so os._exit(0) kills THIS
    worker's MainPID -- the only way systemd (Restart=always) respawns
    rawos.service against new_sha and boot_liveness_commit can resolve the
    pending state written here. Mirrors the /metrics localhost check
    (X-Forwarded-For aware -- nginx /api/ proxies here with real client IP).
    """

    def test_refuses_remote_request(self):
        r = client.post(
            "/internal/self-reload/arm-and-go",
            json={"new_sha": "deadbeef"},
            headers={"X-Forwarded-For": "203.0.113.5"},
        )
        assert r.status_code == 403

    def test_missing_new_sha(self):
        r = client.post(
            "/internal/self-reload/arm-and-go",
            json={},
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        assert r.status_code == 400

    def test_preflight_error_returns_409(self, monkeypatch):
        import rawos.kernel.self_reload as self_reload

        def _raise(*args, **kwargs):
            raise self_reload.SelfReloadPreflightError("boom")

        monkeypatch.setattr(self_reload, "execute_owner_self_reload", _raise)
        r = client.post(
            "/internal/self-reload/arm-and-go",
            json={"new_sha": "deadbeef"},
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        assert r.status_code == 409
        assert "boom" in r.json()["detail"]

    def test_state_error_returns_409(self, monkeypatch):
        import rawos.kernel.self_reload as self_reload

        def _raise(*args, **kwargs):
            raise self_reload.SelfReloadStateError("pending")

        monkeypatch.setattr(self_reload, "execute_owner_self_reload", _raise)
        r = client.post(
            "/internal/self-reload/arm-and-go",
            json={"new_sha": "deadbeef"},
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        assert r.status_code == 409
        assert "pending" in r.json()["detail"]

    def test_calls_execute_owner_self_reload(self, monkeypatch):
        import rawos.kernel.self_reload as self_reload

        calls = []

        def _stub(new_sha, **kwargs):
            calls.append(new_sha)

        monkeypatch.setattr(self_reload, "execute_owner_self_reload", _stub)
        r = client.post(
            "/internal/self-reload/arm-and-go",
            json={"new_sha": "deadbeef"},
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        assert r.status_code == 200
        assert calls == ["deadbeef"]


class TestInternalSelfReloadDebugArmAndSwap:
    """Phase 25 twin-prove -- /internal/self-reload/_debug-arm-and-swap.

    Disabled by default (404). When settings.self_reload_debug_endpoint_enabled
    is True (twin .env only), runs preflight_stage + arm_and_swap directly with
    _revert_cmd overridden to /usr/local/bin/rawos-selfprobe-revert -- never
    execute_owner_self_reload (I-SR6 prod funnel, hardcodes the prod revert
    script which targets /root/rawos + `systemctl restart rawos`).
    """

    def test_disabled_by_default_returns_404(self, monkeypatch):
        import rawos.kernel.self_reload as self_reload

        monkeypatch.setattr(self_reload.settings, "self_reload_debug_endpoint_enabled", False)
        r = client.post(
            "/internal/self-reload/_debug-arm-and-swap",
            json={"new_sha": "deadbeef"},
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        assert r.status_code == 404

    def test_refuses_remote_request(self, monkeypatch):
        import rawos.kernel.self_reload as self_reload

        monkeypatch.setattr(self_reload.settings, "self_reload_debug_endpoint_enabled", True)
        r = client.post(
            "/internal/self-reload/_debug-arm-and-swap",
            json={"new_sha": "deadbeef"},
            headers={"X-Forwarded-For": "203.0.113.5"},
        )
        assert r.status_code == 403

    def test_missing_new_sha(self, monkeypatch):
        import rawos.kernel.self_reload as self_reload

        monkeypatch.setattr(self_reload.settings, "self_reload_debug_endpoint_enabled", True)
        r = client.post(
            "/internal/self-reload/_debug-arm-and-swap",
            json={},
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        assert r.status_code == 400

    def test_preflight_error_returns_409(self, monkeypatch):
        import rawos.kernel.self_reload as self_reload

        monkeypatch.setattr(self_reload.settings, "self_reload_debug_endpoint_enabled", True)

        def _raise(*args, **kwargs):
            raise self_reload.SelfReloadPreflightError("boom")

        monkeypatch.setattr(self_reload, "preflight_stage", _raise)
        r = client.post(
            "/internal/self-reload/_debug-arm-and-swap",
            json={"new_sha": "deadbeef"},
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        assert r.status_code == 409
        assert "boom" in r.json()["detail"]

    def test_state_error_returns_409(self, monkeypatch):
        import rawos.kernel.self_reload as self_reload

        monkeypatch.setattr(self_reload.settings, "self_reload_debug_endpoint_enabled", True)
        snap = self_reload.SelfReloadSnapshot(
            old_sha="OLDSHA", new_sha="deadbeef", state_id="state-1",
            armed_at=0.0, deadman_unit=self_reload.SELF_RELOAD_DEADMAN_UNIT,
            migration_delta=[], venv_frozen_hash="hash",
        )
        monkeypatch.setattr(self_reload, "preflight_stage", lambda *a, **k: snap)

        def _raise(*args, **kwargs):
            raise self_reload.SelfReloadStateError("pending")

        monkeypatch.setattr(self_reload, "arm_and_swap", _raise)
        r = client.post(
            "/internal/self-reload/_debug-arm-and-swap",
            json={"new_sha": "deadbeef"},
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        assert r.status_code == 409
        assert "pending" in r.json()["detail"]

    def test_calls_arm_and_swap_with_selfprobe_revert_cmd(self, monkeypatch):
        import rawos.kernel.self_reload as self_reload

        monkeypatch.setattr(self_reload.settings, "self_reload_debug_endpoint_enabled", True)
        snap = self_reload.SelfReloadSnapshot(
            old_sha="abc1234", new_sha="deadbeef", state_id="state-xyz",
            armed_at=0.0, deadman_unit=self_reload.SELF_RELOAD_DEADMAN_UNIT,
            migration_delta=[], venv_frozen_hash="hash",
        )
        monkeypatch.setattr(self_reload, "preflight_stage", lambda *a, **k: snap)

        calls = []

        def _stub(snap_arg, **kwargs):
            calls.append((snap_arg, kwargs))

        monkeypatch.setattr(self_reload, "arm_and_swap", _stub)
        r = client.post(
            "/internal/self-reload/_debug-arm-and-swap",
            json={"new_sha": "deadbeef"},
            headers={"X-Forwarded-For": "127.0.0.1"},
        )
        assert r.status_code == 200
        assert len(calls) == 1
        passed_snap, kwargs = calls[0]
        assert passed_snap is snap
        assert kwargs["_revert_cmd"] == "/usr/local/bin/rawos-selfprobe-revert abc1234 state-xyz"
