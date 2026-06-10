"""
Phase 16 Pass 2 — TIER enforcement helper tests.

Covers the git-introspection helpers and execute() wrapper added in
rawos/kernel/tools.py (commits ffab93e0 and 31864421) that detect and
revert TIER 0 violations during self-modification of /root/rawos.
See PLAN.md "Phase 16 — Pass 2 — IMPLEMENTED (2026-06-09)".
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest


def _git(*args: str, cwd: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(path: str) -> None:
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@rawos.local", cwd=path)
    _git("config", "user.name", "rawos-test", cwd=path)


# ---------------------------------------------------------------------------
# _in_tier1_allowlist — pure function
# ---------------------------------------------------------------------------

class TestInTier1Allowlist:
    def test_tests_dir_allowed(self):
        from rawos.kernel.tools import _in_tier1_allowlist
        assert _in_tier1_allowlist("tests/test_new_module.py")

    def test_evaluation_dir_allowed(self):
        from rawos.kernel.tools import _in_tier1_allowlist
        assert _in_tier1_allowlist("rawos/evaluation/metrics.py")

    def test_docs_dir_allowed(self):
        from rawos.kernel.tools import _in_tier1_allowlist
        assert _in_tier1_allowlist("docs/architecture.md")

    def test_exact_prefix_with_no_trailing_slash_allowed(self):
        from rawos.kernel.tools import _in_tier1_allowlist
        assert _in_tier1_allowlist("rawos/manifester")

    def test_tier0_api_path_blocked(self):
        from rawos.kernel.tools import _in_tier1_allowlist
        assert not _in_tier1_allowlist("rawos/api/app.py")

    def test_tier0_kernel_tools_blocked(self):
        from rawos.kernel.tools import _in_tier1_allowlist
        assert not _in_tier1_allowlist("rawos/kernel/tools.py")

    def test_similar_prefix_not_falsely_matched(self):
        # "rawos/studyx/" must NOT match the "rawos/study/" prefix
        from rawos.kernel.tools import _in_tier1_allowlist
        assert not _in_tier1_allowlist("rawos/studyx/evil.py")


# ---------------------------------------------------------------------------
# _diff_paths — pure function
# ---------------------------------------------------------------------------

class TestDiffPaths:
    def test_new_dirty_path_detected(self):
        from rawos.kernel.tools import _diff_paths
        assert _diff_paths({}, {"a.py": "M "}) == {"a.py"}

    def test_unchanged_status_not_flagged(self):
        from rawos.kernel.tools import _diff_paths
        before = {"data/rawos.db": "M "}
        after = {"data/rawos.db": "M "}
        assert _diff_paths(before, after) == set()

    def test_reverted_to_clean_detected(self):
        from rawos.kernel.tools import _diff_paths
        before = {"a.py": "M "}
        after: dict[str, str] = {}
        assert _diff_paths(before, after) == {"a.py"}

    def test_status_change_on_already_dirty_path_detected(self):
        from rawos.kernel.tools import _diff_paths
        before = {"a.py": " M"}
        after = {"a.py": "MM"}
        assert _diff_paths(before, after) == {"a.py"}

    def test_independent_path_untouched(self):
        from rawos.kernel.tools import _diff_paths
        before = {"data/rawos.db": "M "}
        after = {"data/rawos.db": "M ", "rawos/api/app.py": " M"}
        assert _diff_paths(before, after) == {"rawos/api/app.py"}


# ---------------------------------------------------------------------------
# _is_rawos_source_tree — git introspection
# ---------------------------------------------------------------------------

class TestIsRawosSourceTree:
    def test_unrelated_repo_is_not_rawos(self, tmp_path):
        from rawos.kernel.tools import _is_rawos_source_tree
        _init_repo(str(tmp_path))
        assert asyncio.run(_is_rawos_source_tree(str(tmp_path))) is False

    def test_non_git_dir_is_not_rawos(self, tmp_path):
        from rawos.kernel.tools import _is_rawos_source_tree
        assert asyncio.run(_is_rawos_source_tree(str(tmp_path))) is False


# ---------------------------------------------------------------------------
# _git_status_porcelain — git introspection
# ---------------------------------------------------------------------------

class TestGitStatusPorcelain:
    def test_clean_repo_is_empty(self, tmp_path):
        from rawos.kernel.tools import _git_status_porcelain
        _init_repo(str(tmp_path))
        (tmp_path / "a.txt").write_text("a")
        _git("add", "a.txt", cwd=str(tmp_path))
        _git("commit", "-qm", "init", cwd=str(tmp_path))
        assert asyncio.run(_git_status_porcelain(str(tmp_path))) == {}

    def test_untracked_file_detected(self, tmp_path):
        from rawos.kernel.tools import _git_status_porcelain
        _init_repo(str(tmp_path))
        (tmp_path / "a.txt").write_text("a")
        _git("add", "a.txt", cwd=str(tmp_path))
        _git("commit", "-qm", "init", cwd=str(tmp_path))
        (tmp_path / "new.txt").write_text("new")
        status = asyncio.run(_git_status_porcelain(str(tmp_path)))
        assert status == {"new.txt": "??"}

    def test_modified_tracked_file_detected(self, tmp_path):
        from rawos.kernel.tools import _git_status_porcelain
        _init_repo(str(tmp_path))
        (tmp_path / "a.txt").write_text("a")
        _git("add", "a.txt", cwd=str(tmp_path))
        _git("commit", "-qm", "init", cwd=str(tmp_path))
        (tmp_path / "a.txt").write_text("changed")
        status = asyncio.run(_git_status_porcelain(str(tmp_path)))
        assert status == {"a.txt": " M"}

    def test_rename_split_into_two_entries(self, tmp_path):
        from rawos.kernel.tools import _git_status_porcelain
        _init_repo(str(tmp_path))
        (tmp_path / "old.txt").write_text("a")
        _git("add", "old.txt", cwd=str(tmp_path))
        _git("commit", "-qm", "init", cwd=str(tmp_path))
        _git("mv", "old.txt", "new.txt", cwd=str(tmp_path))
        status = asyncio.run(_git_status_porcelain(str(tmp_path)))
        assert status["new.txt"] == "RM" or status["new.txt"][0] == "R"
        assert status["old.txt"] == "D "

    def test_non_git_dir_returns_empty(self, tmp_path):
        from rawos.kernel.tools import _git_status_porcelain
        assert asyncio.run(_git_status_porcelain(str(tmp_path))) == {}


# ---------------------------------------------------------------------------
# _git_checkout_restore — git introspection, mutates working tree
# ---------------------------------------------------------------------------

class TestGitCheckoutRestore:
    def test_restores_modified_tracked_file(self, tmp_path):
        from rawos.kernel.tools import _git_status_porcelain, _git_checkout_restore
        _init_repo(str(tmp_path))
        (tmp_path / "a.txt").write_text("original")
        _git("add", "a.txt", cwd=str(tmp_path))
        _git("commit", "-qm", "init", cwd=str(tmp_path))
        (tmp_path / "a.txt").write_text("violation")

        asyncio.run(_git_checkout_restore(str(tmp_path), "a.txt"))

        assert (tmp_path / "a.txt").read_text() == "original"
        assert asyncio.run(_git_status_porcelain(str(tmp_path))) == {}

    def test_removes_new_untracked_file(self, tmp_path):
        from rawos.kernel.tools import _git_status_porcelain, _git_checkout_restore
        _init_repo(str(tmp_path))
        (tmp_path / "a.txt").write_text("a")
        _git("add", "a.txt", cwd=str(tmp_path))
        _git("commit", "-qm", "init", cwd=str(tmp_path))
        (tmp_path / "evil.py").write_text("malicious")

        asyncio.run(_git_checkout_restore(str(tmp_path), "evil.py"))

        assert not (tmp_path / "evil.py").exists()
        assert asyncio.run(_git_status_porcelain(str(tmp_path))) == {}

    def test_removes_new_staged_file(self, tmp_path):
        from rawos.kernel.tools import _git_status_porcelain, _git_checkout_restore
        _init_repo(str(tmp_path))
        (tmp_path / "a.txt").write_text("a")
        _git("add", "a.txt", cwd=str(tmp_path))
        _git("commit", "-qm", "init", cwd=str(tmp_path))
        (tmp_path / "evil.py").write_text("malicious")
        _git("add", "evil.py", cwd=str(tmp_path))

        asyncio.run(_git_checkout_restore(str(tmp_path), "evil.py"))

        assert not (tmp_path / "evil.py").exists()
        assert asyncio.run(_git_status_porcelain(str(tmp_path))) == {}


# ---------------------------------------------------------------------------
# execute() wrapper integration tests (Pass 2 step c)
#
# _RAWOS_GIT_COMMON_DIR is monkeypatched to point at a throwaway tmp_path
# repo so _is_rawos_source_tree treats it as "rawos's own source tree"
# without touching /root/rawos. _targets_rawos_own_repo is monkeypatched
# separately for the live-tree case, since it compares against the
# hardcoded "/root/rawos" toplevel.
# ---------------------------------------------------------------------------

class TestExecuteWrapper:
    def _setup_repo(self, tmp_path):
        _init_repo(str(tmp_path))
        (tmp_path / "rawos" / "api").mkdir(parents=True)
        (tmp_path / "rawos" / "evaluation").mkdir(parents=True)
        (tmp_path / "tests").mkdir(parents=True)
        (tmp_path / "docs").mkdir(parents=True)
        (tmp_path / "rawos" / "api" / "app.py").write_text("# tier0\n")
        (tmp_path / "rawos" / "evaluation" / "metrics.py").write_text("# tier1 module\n")
        (tmp_path / "tests" / "__init__.py").write_text("")
        _git("add", "-A", cwd=str(tmp_path))
        _git("commit", "-qm", "init", cwd=str(tmp_path))
        return tmp_path

    def _patch_common_dir(self, monkeypatch, repo):
        import rawos.kernel.tools as tools
        monkeypatch.setattr(tools, "_RAWOS_GIT_COMMON_DIR", str(repo / ".git"))

    def _head(self, repo):
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True,
        ).stdout.strip()

    def test_live_tree_mutating_tool_refused(self, tmp_path, monkeypatch):
        from rawos.kernel.tools import execute
        import rawos.kernel.tools as tools
        repo = self._setup_repo(tmp_path)

        async def fake_targets_rawos(workdir):
            return True

        monkeypatch.setattr(tools, "_targets_rawos_own_repo", fake_targets_rawos)

        result = asyncio.run(execute(
            "write_file", {"path": "rawos/api/app.py", "content": "EVIL"}, str(repo),
        ))
        assert not result.success
        assert "refusing" in result.output
        assert (repo / "rawos" / "api" / "app.py").read_text() == "# tier0\n"

    def test_worktree_tier0_write_reverted(self, tmp_path, monkeypatch):
        from rawos.kernel.tools import execute
        repo = self._setup_repo(tmp_path)
        self._patch_common_dir(monkeypatch, repo)

        result = asyncio.run(execute(
            "write_file", {"path": "rawos/api/app.py", "content": "EVIL"}, str(repo),
        ))
        assert not result.success
        assert "TIER VIOLATION" in result.output
        assert (repo / "rawos" / "api" / "app.py").read_text() == "# tier0\n"

    def test_worktree_tier1_tests_write_allowed(self, tmp_path, monkeypatch):
        from rawos.kernel.tools import execute
        repo = self._setup_repo(tmp_path)
        self._patch_common_dir(monkeypatch, repo)

        result = asyncio.run(execute(
            "write_file", {"path": "tests/test_new.py", "content": "def test_x(): assert True"}, str(repo),
        ))
        assert result.success
        assert (repo / "tests" / "test_new.py").exists()

    def test_worktree_commit_smuggling_tier0_reverted(self, tmp_path, monkeypatch):
        from rawos.kernel.tools import execute
        repo = self._setup_repo(tmp_path)
        self._patch_common_dir(monkeypatch, repo)
        _git("checkout", "-qb", "rawos/test-branch", cwd=str(repo))
        before_head = self._head(repo)

        cmd = (
            "echo EVIL >> rawos/api/app.py && git add -A && "
            "git -c user.name=t -c user.email=t@t.com commit -qm smuggle"
        )
        result = asyncio.run(execute("bash", {"command": cmd}, str(repo)))

        assert not result.success
        assert "TIER VIOLATION" in result.output
        assert self._head(repo) == before_head
        assert (repo / "rawos" / "api" / "app.py").read_text() == "# tier0\n"

    def test_worktree_mixed_commit_tier0_reverted_tier1_survives(self, tmp_path, monkeypatch):
        from rawos.kernel.tools import execute
        repo = self._setup_repo(tmp_path)
        self._patch_common_dir(monkeypatch, repo)
        _git("checkout", "-qb", "rawos/test-branch", cwd=str(repo))
        before_head = self._head(repo)

        cmd = (
            "echo EVIL >> rawos/api/app.py && "
            "echo 'def test_y(): assert True' > tests/test_y.py && "
            "git add -A && git -c user.name=t -c user.email=t@t.com commit -qm mixed"
        )
        result = asyncio.run(execute("bash", {"command": cmd}, str(repo)))

        assert not result.success
        assert "rawos/api/app.py" in result.output
        assert "tests/test_y.py" not in result.output
        assert self._head(repo) == before_head
        assert (repo / "rawos" / "api" / "app.py").read_text() == "# tier0\n"
        assert (repo / "tests" / "test_y.py").exists()

    def test_bootstrap_blocks_tier1_module_source_edit_without_tests(self, tmp_path, monkeypatch):
        from rawos.kernel.tools import execute
        repo = self._setup_repo(tmp_path)
        self._patch_common_dir(monkeypatch, repo)

        result = asyncio.run(execute(
            "write_file", {"path": "rawos/evaluation/metrics.py", "content": "# edited"}, str(repo),
        ))
        assert not result.success
        assert "TIER VIOLATION" in result.output
        assert (repo / "rawos" / "evaluation" / "metrics.py").read_text() == "# tier1 module\n"

    def test_bootstrap_unlocks_after_module_has_tests(self, tmp_path, monkeypatch):
        from rawos.kernel.tools import execute
        repo = self._setup_repo(tmp_path)
        self._patch_common_dir(monkeypatch, repo)

        (repo / "tests" / "test_evaluation_metrics.py").write_text("def test_x(): assert True")
        _git("add", "-A", cwd=str(repo))
        _git("commit", "-qm", "add evaluation tests", cwd=str(repo))

        result = asyncio.run(execute(
            "write_file", {"path": "rawos/evaluation/metrics.py", "content": "# edited"}, str(repo),
        ))
        assert result.success
        assert (repo / "rawos" / "evaluation" / "metrics.py").read_text() == "# edited"

    def test_non_rawos_repo_passthrough_unchanged(self, tmp_path):
        from rawos.kernel.tools import execute
        repo = self._setup_repo(tmp_path)

        result = asyncio.run(execute(
            "write_file", {"path": "rawos/api/app.py", "content": "y"}, str(repo),
        ))
        assert result.success
        assert (repo / "rawos" / "api" / "app.py").read_text() == "y"
