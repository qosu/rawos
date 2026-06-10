"""
Stage 1 — SERVER_SCAN isolation tests.

Covers the additions made to stop the SERVER_SCAN cost/pollution loop:
- kernel/tools.py: read-only systemctl/journalctl whitelist additions to
  _is_bash_readonly_safe (still rejects mutating subcommands/flags).
- kernel/worktree.py: create_worktree/remove_worktree disposable git
  worktree lifecycle.
- scheduler/proactive.py: _manifest_target redirect for SERVER_SCAN runs,
  _get_tools_for_server_scan toolset.
"""
from __future__ import annotations

import subprocess

import pytest


def _git(*args: str, cwd: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _git_out(*args: str, cwd: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _init_repo(path: str) -> None:
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@rawos.local", cwd=path)
    _git("config", "user.name", "rawos-test", cwd=path)
    (path_obj := __import__("pathlib").Path(path) / "README.md").write_text("init\n")
    _git("add", "README.md", cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)


# ---------------------------------------------------------------------------
# _is_bash_readonly_safe — systemctl whitelist additions
# ---------------------------------------------------------------------------

class TestBashReadonlySystemctl:
    def test_status_allowed(self):
        from rawos.kernel.tools import _is_bash_readonly_safe
        assert _is_bash_readonly_safe("systemctl status research-foundry.service")

    def test_show_allowed(self):
        from rawos.kernel.tools import _is_bash_readonly_safe
        assert _is_bash_readonly_safe("systemctl show research-foundry.service")

    def test_is_active_allowed(self):
        from rawos.kernel.tools import _is_bash_readonly_safe
        assert _is_bash_readonly_safe("systemctl is-active rawos.service")

    def test_is_failed_allowed(self):
        from rawos.kernel.tools import _is_bash_readonly_safe
        assert _is_bash_readonly_safe("systemctl is-failed rawos.service")

    def test_list_units_allowed(self):
        from rawos.kernel.tools import _is_bash_readonly_safe
        assert _is_bash_readonly_safe("systemctl list-units --failed")

    def test_restart_rejected(self):
        from rawos.kernel.tools import _is_bash_readonly_safe
        assert not _is_bash_readonly_safe("systemctl restart research-foundry.service")

    def test_stop_rejected(self):
        from rawos.kernel.tools import _is_bash_readonly_safe
        assert not _is_bash_readonly_safe("systemctl stop rawos.service")

    def test_start_rejected(self):
        from rawos.kernel.tools import _is_bash_readonly_safe
        assert not _is_bash_readonly_safe("systemctl start rawos.service")

    def test_enable_rejected(self):
        from rawos.kernel.tools import _is_bash_readonly_safe
        assert not _is_bash_readonly_safe("systemctl enable rawos.service")

    def test_daemon_reload_rejected(self):
        from rawos.kernel.tools import _is_bash_readonly_safe
        assert not _is_bash_readonly_safe("systemctl daemon-reload")


# ---------------------------------------------------------------------------
# _is_bash_readonly_safe — journalctl whitelist additions
# ---------------------------------------------------------------------------

class TestBashReadonlyJournalctl:
    def test_unit_lookup_allowed(self):
        from rawos.kernel.tools import _is_bash_readonly_safe
        assert _is_bash_readonly_safe("journalctl -u research-foundry.service -n 50")

    def test_follow_short_flag_rejected(self):
        from rawos.kernel.tools import _is_bash_readonly_safe
        assert not _is_bash_readonly_safe("journalctl -u rawos.service -f")

    def test_follow_long_flag_rejected(self):
        from rawos.kernel.tools import _is_bash_readonly_safe
        assert not _is_bash_readonly_safe("journalctl -u rawos.service --follow")

    def test_vacuum_size_rejected(self):
        from rawos.kernel.tools import _is_bash_readonly_safe
        assert not _is_bash_readonly_safe("journalctl --vacuum-size=100M")

    def test_vacuum_time_rejected(self):
        from rawos.kernel.tools import _is_bash_readonly_safe
        assert not _is_bash_readonly_safe("journalctl --vacuum-time=1d")

    def test_rotate_rejected(self):
        from rawos.kernel.tools import _is_bash_readonly_safe
        assert not _is_bash_readonly_safe("journalctl --rotate")


# ---------------------------------------------------------------------------
# kernel/worktree.py — create_worktree / remove_worktree
# ---------------------------------------------------------------------------

class TestWorktreeLifecycle:
    @pytest.mark.asyncio
    async def test_create_and_remove_worktree(self, tmp_path):
        from rawos.kernel.worktree import create_worktree, remove_worktree

        repo = tmp_path / "origin-repo"
        repo.mkdir()
        _init_repo(str(repo))

        worktree_path = await create_worktree(str(repo))
        assert worktree_path is not None
        assert (__import__("pathlib").Path(worktree_path) / "README.md").exists()
        assert (__import__("pathlib").Path(worktree_path) / ".git").exists()

        await remove_worktree(worktree_path)
        assert not __import__("pathlib").Path(worktree_path).exists()

        # Removing the worktree must not break the origin repo.
        result = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(repo),
            check=True, capture_output=True, text=True,
        )
        assert result.stdout.strip() == ""

    @pytest.mark.asyncio
    async def test_get_head_sha_matches_repo_head(self, tmp_path):
        from rawos.kernel.worktree import create_worktree, get_head_sha, remove_worktree

        repo = tmp_path / "origin-repo-sha"
        repo.mkdir()
        _init_repo(str(repo))
        expected_sha = _git_out("rev-parse", "HEAD", cwd=str(repo))

        worktree_path = await create_worktree(str(repo))
        assert worktree_path is not None
        try:
            assert await get_head_sha(worktree_path) == expected_sha
        finally:
            await remove_worktree(worktree_path)

    @pytest.mark.asyncio
    async def test_get_head_sha_nonexistent_path_returns_none(self, tmp_path):
        from rawos.kernel.worktree import get_head_sha

        assert await get_head_sha(str(tmp_path / "does-not-exist")) is None

    @pytest.mark.asyncio
    async def test_create_worktree_non_repo_returns_none(self, tmp_path):
        from rawos.kernel.worktree import create_worktree

        not_a_repo = tmp_path / "plain-dir"
        not_a_repo.mkdir()

        assert await create_worktree(str(not_a_repo)) is None

    @pytest.mark.asyncio
    async def test_remove_worktree_refuses_outside_root(self, tmp_path):
        from rawos.kernel.worktree import remove_worktree, WORKTREE_ROOT

        outside = tmp_path / "not-a-worktree"
        outside.mkdir()
        assert not str(outside.resolve()).startswith(str(WORKTREE_ROOT.resolve()))

        await remove_worktree(str(outside))

        # Path must be untouched — function refused to remove it.
        assert outside.exists()

    @pytest.mark.asyncio
    async def test_branch_committed_in_worktree_visible_in_origin(self, tmp_path):
        from rawos.kernel.worktree import create_worktree, remove_worktree

        repo = tmp_path / "origin-repo2"
        repo.mkdir()
        _init_repo(str(repo))

        worktree_path = await create_worktree(str(repo))
        assert worktree_path is not None

        _git("checkout", "-q", "-b", "rawos/fix-test", cwd=worktree_path)
        (__import__("pathlib").Path(worktree_path) / "fix.txt").write_text("fix\n")
        _git("add", "fix.txt", cwd=worktree_path)
        _git("commit", "-q", "-m", "rawos: fix test", cwd=worktree_path)

        await remove_worktree(worktree_path)

        branches = subprocess.run(
            ["git", "branch", "--list", "rawos/fix-test"], cwd=str(repo),
            check=True, capture_output=True, text=True,
        )
        assert "rawos/fix-test" in branches.stdout


# ---------------------------------------------------------------------------
# scheduler/proactive.py — _manifest_target / _get_tools_for_server_scan
# ---------------------------------------------------------------------------

class TestManifestTarget:
    def test_server_scan_redirects_under_rawos_data_manifests(self):
        from rawos.scheduler.proactive import _manifest_target
        target = _manifest_target("/root/liveproof-agent", "SERVER_SCAN")
        assert target is not None
        assert target.endswith("/data/manifests/liveproof-agent")
        assert target.startswith("/root/rawos/")

    def test_non_server_scan_returns_none(self):
        from rawos.scheduler.proactive import _manifest_target
        assert _manifest_target("/root/some/project", "ENTITY_AUTONOMOUS") is None
        assert _manifest_target("/root/some/project", None) is None


class TestServerScanToolset:
    def test_includes_write_and_git_tools(self):
        from rawos.scheduler.proactive import _get_tools_for_server_scan
        names = {t["function"]["name"] for t in _get_tools_for_server_scan()}
        assert names == {
            "bash_readonly", "read_file", "list_files", "write_file",
            "git_branch", "git_commit",
        }

    def test_excludes_full_shell_and_deploy(self):
        from rawos.scheduler.proactive import _get_tools_for_server_scan
        names = {t["function"]["name"] for t in _get_tools_for_server_scan()}
        assert "bash" not in names
        assert "deploy" not in names
        assert "fetch_url" not in names
