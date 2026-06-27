"""
tests/test_sandbox_isolation.py — SHP.3 sandbox isolation verification.

Tests:
  - Unit tests: verify docker_cmd construction includes all SHP.3 hardening flags.
  - Integration tests (require Docker): marked @pytest.mark.docker; skipped if Docker absent.
    Run with: pytest -m docker tests/test_sandbox_isolation.py
    They actually execute containers and verify isolation properties.
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────

def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=5, check=True)
        return True
    except Exception:
        return False


requires_docker = pytest.mark.skipif(
    not _docker_available(), reason="Docker daemon not available"
)


# ── import under test ─────────────────────────────────────────────────────────

# Import lazily so the test file is parseable even if rawos is not installed
def _import_sandbox():
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from rawos.kernel.sandbox import run_bash_in_container, DOCKER_IMAGE  # noqa: F401
    return run_bash_in_container, DOCKER_IMAGE


# ── unit tests: command construction ─────────────────────────────────────────

class TestContainerCommandConstruction:
    """Verify run_bash_in_container builds a hardened docker command.

    These are pure unit tests — they patch asyncio.create_subprocess_exec and
    capture the arguments rather than running a real container.
    """

    @pytest.fixture()
    def captured_docker_cmd(self, tmp_path) -> list[str]:
        """Run run_bash_in_container with a mocked subprocess and return captured args."""
        run_bash_in_container, _ = _import_sandbox()

        captured: list[list[str]] = []

        async def _fake_exec(*args, **kwargs):
            captured.append(list(args))
            mock_proc = AsyncMock()
            mock_proc.returncode = 0
            mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
            return mock_proc

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
            asyncio.run(run_bash_in_container("echo ok", str(tmp_path)))

        assert captured, "create_subprocess_exec was never called"
        return captured[0]  # list of positional args = docker argv

    def test_cap_drop_all_present(self, captured_docker_cmd):
        """--cap-drop ALL must be in the command (I-SEC2: minimum capabilities)."""
        cmd = captured_docker_cmd
        assert "--cap-drop" in cmd, "--cap-drop flag missing from docker command"
        cap_idx = cmd.index("--cap-drop")
        assert cmd[cap_idx + 1] == "ALL", f"Expected --cap-drop ALL, got {cmd[cap_idx+1]!r}"

    def test_read_only_present(self, captured_docker_cmd):
        """--read-only must be present (prevents container rootfs modification)."""
        assert "--read-only" in captured_docker_cmd, "--read-only missing from docker command"

    def test_tmpfs_tmp_present(self, captured_docker_cmd):
        """A writable --tmpfs /tmp must be present alongside --read-only."""
        cmd = captured_docker_cmd
        # Could be "--tmpfs" "/tmp:..." or "--tmpfs=/tmp:..."
        has_tmpfs = (
            "--tmpfs" in cmd
            or any(a.startswith("--tmpfs=") for a in cmd)
        )
        assert has_tmpfs, "--tmpfs flag missing — container /tmp will be unwritable with --read-only"

    def test_network_none_present(self, captured_docker_cmd):
        """--network none must still be present (no regression)."""
        cmd = captured_docker_cmd
        assert "--network" in cmd
        net_idx = cmd.index("--network")
        assert cmd[net_idx + 1] == "none", "Expected --network none"

    def test_no_new_privileges_present(self, captured_docker_cmd):
        """--security-opt no-new-privileges must still be present (no regression)."""
        cmd = captured_docker_cmd
        assert "--security-opt" in cmd
        sec_opts = [
            cmd[i + 1] for i, a in enumerate(cmd) if a == "--security-opt"
        ]
        assert any("no-new-privileges" in o for o in sec_opts), (
            f"no-new-privileges not in security opts: {sec_opts}"
        )

    def test_rm_flag_present(self, captured_docker_cmd):
        """--rm must be present — containers must be ephemeral, no leftover state."""
        assert "--rm" in captured_docker_cmd, "--rm missing — containers leak on exit"

    def test_memory_limit_present(self, captured_docker_cmd):
        """--memory limit must be present (resource exhaustion protection)."""
        assert "--memory" in captured_docker_cmd, "--memory limit missing"

    def test_pids_limit_present(self, captured_docker_cmd):
        """--pids-limit must be present (fork-bomb protection)."""
        assert "--pids-limit" in captured_docker_cmd, "--pids-limit missing"

    def test_workdir_bind_mount_present(self, captured_docker_cmd, tmp_path):
        """User workspace bind-mount must be present and point to the correct path."""
        cmd = captured_docker_cmd
        assert "-v" in cmd
        vol_idx = cmd.index("-v")
        vol_spec = cmd[vol_idx + 1]
        assert str(tmp_path) in vol_spec, (
            f"Workspace {tmp_path} not in volume spec: {vol_spec!r}"
        )
        assert "/workspace" in vol_spec


# ── integration tests: actual isolation properties ────────────────────────────

class TestContainerIsolationIntegration:
    """Live container tests — verify actual isolation, not just flag presence.
    Requires Docker daemon. Marked @requires_docker."""

    @requires_docker
    def test_network_is_blocked(self, tmp_path):
        """Container must not reach external network."""
        run_bash_in_container, _ = _import_sandbox()
        result = asyncio.run(
            run_bash_in_container(
                "curl -s --max-time 2 http://1.1.1.1 || ping -c1 -W1 1.1.1.1 || echo NETWORK_BLOCKED",
                str(tmp_path),
            )
        )
        combined = (result.stdout + result.stderr).lower()
        assert "network_blocked" in combined or result.exit_code != 0, (
            "Container appears to have external network access — --network none may be broken"
        )

    @requires_docker
    def test_metadata_endpoint_is_blocked(self, tmp_path):
        """Container must not reach cloud metadata endpoint (SSRF I-SEC8 at container level)."""
        run_bash_in_container, _ = _import_sandbox()
        result = asyncio.run(
            run_bash_in_container(
                "curl -sf --max-time 2 http://169.254.169.254/latest/meta-data/ || echo METADATA_BLOCKED",
                str(tmp_path),
            )
        )
        combined = result.stdout + result.stderr
        assert "METADATA_BLOCKED" in combined or result.exit_code != 0, (
            "Container reached cloud metadata endpoint — critical SSRF risk"
        )

    @requires_docker
    def test_no_privileged_capability(self, tmp_path):
        """Container must not have CAP_SYS_ADMIN or similar dangerous capabilities."""
        run_bash_in_container, _ = _import_sandbox()
        # Attempt a privileged operation that requires CAP_SYS_ADMIN: mount
        result = asyncio.run(
            run_bash_in_container(
                "mount -t tmpfs tmpfs /mnt 2>&1 || echo CAP_DENIED",
                str(tmp_path),
            )
        )
        combined = result.stdout + result.stderr
        assert "CAP_DENIED" in combined or "permission denied" in combined.lower() or "operation not permitted" in combined.lower(), (
            "Container appears to have mount capability — --cap-drop ALL may not be effective"
        )

    @requires_docker
    def test_workspace_is_writable(self, tmp_path):
        """Workspace bind-mount must still be writable even with --read-only rootfs."""
        run_bash_in_container, _ = _import_sandbox()
        result = asyncio.run(
            run_bash_in_container(
                "echo 'hello' > /workspace/test_write.txt && cat /workspace/test_write.txt",
                str(tmp_path),
            )
        )
        assert result.exit_code == 0, f"Workspace write failed: {result.stderr}"
        assert "hello" in result.stdout

    @requires_docker
    def test_container_rootfs_is_read_only(self, tmp_path):
        """Container rootfs (outside /workspace and /tmp) must be read-only."""
        run_bash_in_container, _ = _import_sandbox()
        result = asyncio.run(
            run_bash_in_container(
                "echo 'write' > /usr/local/bin/evil 2>&1 || echo ROOTFS_READONLY",
                str(tmp_path),
            )
        )
        combined = result.stdout + result.stderr
        assert "ROOTFS_READONLY" in combined or "read-only" in combined.lower() or result.exit_code != 0, (
            "Container rootfs appears to be writable — --read-only may not be active"
        )

    @requires_docker
    def test_tmp_is_writable(self, tmp_path):
        """Container /tmp must be writable (tmpfs overlay over read-only rootfs)."""
        run_bash_in_container, _ = _import_sandbox()
        result = asyncio.run(
            run_bash_in_container(
                "echo 'tmp_write' > /tmp/test.txt && cat /tmp/test.txt",
                str(tmp_path),
            )
        )
        assert result.exit_code == 0, f"/tmp not writable: {result.stderr}"
        assert "tmp_write" in result.stdout

    @requires_docker
    def test_tenant_workspace_isolation(self, tmp_path):
        """Container must only see its own workspace — not other tenants' paths."""
        import tempfile
        run_bash_in_container, _ = _import_sandbox()
        # Create a "other tenant" dir at host level
        with tempfile.TemporaryDirectory() as other_tenant_dir:
            secret_file = Path(other_tenant_dir) / "other_tenant_secret.txt"
            secret_file.write_text("SECRET_DATA")
            # Container for our workspace must not reach the other dir
            result = asyncio.run(
                run_bash_in_container(
                    f"cat {other_tenant_dir}/other_tenant_secret.txt 2>&1 || echo TENANT_ISOLATED",
                    str(tmp_path),
                )
            )
            combined = result.stdout + result.stderr
            assert "TENANT_ISOLATED" in combined or "no such file" in combined.lower(), (
                "Container can read other tenant workspace path — tenant isolation breach"
            )
