"""
Phase 16 step d — the self-probe loop ships DORMANT (commit 552b752e).
settings.self_probe_enabled defaults to False; while disabled,
rawos_self_probe_loop() logs once and returns immediately — no loop,
no sleep, no worktree side effects. See PLAN.md Pass 2 — IMPLEMENTED.
"""
import asyncio

from rawos.config import settings
from rawos.scheduler.proactive import rawos_self_probe_loop


def test_self_probe_disabled_by_default():
    assert settings.self_probe_enabled is False


def test_self_probe_loop_returns_immediately_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "self_probe_enabled", False)

    async def _run_with_timeout():
        await asyncio.wait_for(rawos_self_probe_loop(), timeout=2.0)

    asyncio.run(_run_with_timeout())
# Self-probe cycle tests — Phase 16 Step B
# Append to tests/test_self_probe.py

import subprocess
from pathlib import Path

import pytest

import rawos.kernel.worktree as _worktree_mod
import rawos.scheduler.proactive as _proactive_mod
from rawos.scheduler.proactive import _run_self_probe_cycle


# ── helpers ────────────────────────────────────────────────────────────────

def _g(cmd: str, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd.split(), cwd=cwd, check=True, capture_output=True, text=True
    )


def _setup_probe_repo(tmp_path: Path) -> tuple[str, str]:
    """Create a minimal git repo + worktree root, both under tmp_path."""
    repo = tmp_path / "rawos"
    repo.mkdir()
    _g("git init", str(repo))
    _g("git config user.email test@t.com", str(repo))
    _g("git config user.name Test", str(repo))
    (repo / "README.md").write_text("rawos\n")
    _g("git add .", str(repo))
    _g("git commit -m init", str(repo))
    wt_root = tmp_path / "worktrees"
    wt_root.mkdir()
    return str(repo), str(wt_root)


def _mock_db(monkeypatch) -> None:
    """Silence all DB calls so tests need no database."""
    monkeypatch.setattr(_proactive_mod.db, "create_intent", lambda r: None)
    monkeypatch.setattr(_proactive_mod.db, "create_agent", lambda r: None)
    monkeypatch.setattr(
        _proactive_mod.db, "update_intent", lambda *a, **kw: None
    )


def _mock_agent(monkeypatch, *, captured: list[str] | None = None, raise_exc: Exception | None = None) -> None:
    """Replace agent_loop.run with a lightweight async-generator stub."""

    async def _stub(**kwargs):
        if captured is not None:
            captured.append(kwargs.get("workdir", ""))
        if raise_exc is not None:
            raise raise_exc
        if False:
            yield  # makes this an async generator

    monkeypatch.setattr(_proactive_mod.agent_loop, "run", _stub)


# ── tests ──────────────────────────────────────────────────────────────────

class TestSelfProbeCycle:
    """_run_self_probe_cycle() — Phase 16 Step B contract."""

    def test_workdir_differs_from_live_repo(self, tmp_path, monkeypatch):
        """agent_loop receives workdir != the live rawos repo path."""
        import asyncio
        repo_path, wt_root = _setup_probe_repo(tmp_path)
        monkeypatch.setattr(_proactive_mod, "_SELF_PROBE_RAWOS_REPO", repo_path)
        monkeypatch.setattr(_worktree_mod, "WORKTREE_ROOT", Path(wt_root))
        captured: list[str] = []
        _mock_db(monkeypatch)
        _mock_agent(monkeypatch, captured=captured)

        asyncio.run(_run_self_probe_cycle())

        assert captured, "agent_loop.run never called"
        assert Path(captured[0]).resolve() != Path(repo_path).resolve(), (
            f"workdir={captured[0]!r} must differ from live repo {repo_path!r}"
        )

    def test_branch_name_is_rawos_self_improve(self, tmp_path, monkeypatch):
        """Branch created in worktree must match rawos/self-improve-* pattern."""
        import asyncio
        repo_path, wt_root = _setup_probe_repo(tmp_path)
        monkeypatch.setattr(_proactive_mod, "_SELF_PROBE_RAWOS_REPO", repo_path)
        monkeypatch.setattr(_worktree_mod, "WORKTREE_ROOT", Path(wt_root))
        _mock_db(monkeypatch)
        _mock_agent(monkeypatch)

        asyncio.run(_run_self_probe_cycle())

        # Branch must survive worktree removal (shared object store).
        result = _g("git branch", repo_path)
        branches = result.stdout
        assert any(
            "rawos/self-improve-" in b for b in branches.splitlines()
        ), f"no rawos/self-improve-* branch in origin:\n{branches}"

    def test_master_ref_unchanged(self, tmp_path, monkeypatch):
        """HEAD of origin repo must not move during a self-probe cycle."""
        import asyncio
        repo_path, wt_root = _setup_probe_repo(tmp_path)
        monkeypatch.setattr(_proactive_mod, "_SELF_PROBE_RAWOS_REPO", repo_path)
        monkeypatch.setattr(_worktree_mod, "WORKTREE_ROOT", Path(wt_root))
        _mock_db(monkeypatch)
        _mock_agent(monkeypatch)

        before = _g("git rev-parse HEAD", repo_path).stdout.strip()
        asyncio.run(_run_self_probe_cycle())
        after = _g("git rev-parse HEAD", repo_path).stdout.strip()

        assert before == after, f"HEAD moved: {before} -> {after}"

    def test_worktree_cleaned_up_on_success(self, tmp_path, monkeypatch):
        """Worktree directory must not exist after a successful cycle."""
        import asyncio
        repo_path, wt_root = _setup_probe_repo(tmp_path)
        monkeypatch.setattr(_proactive_mod, "_SELF_PROBE_RAWOS_REPO", repo_path)
        monkeypatch.setattr(_worktree_mod, "WORKTREE_ROOT", Path(wt_root))
        captured: list[str] = []
        _mock_db(monkeypatch)
        _mock_agent(monkeypatch, captured=captured)

        asyncio.run(_run_self_probe_cycle())

        assert captured, "agent_loop.run never called"
        assert not Path(captured[0]).exists(), (
            f"worktree not removed after success: {captured[0]}"
        )

    def test_worktree_cleaned_up_on_agent_exception(self, tmp_path, monkeypatch):
        """Worktree must be removed even when the agent raises."""
        import asyncio
        repo_path, wt_root = _setup_probe_repo(tmp_path)
        monkeypatch.setattr(_proactive_mod, "_SELF_PROBE_RAWOS_REPO", repo_path)
        monkeypatch.setattr(_worktree_mod, "WORKTREE_ROOT", Path(wt_root))
        captured: list[str] = []
        _mock_db(monkeypatch)
        _mock_agent(monkeypatch, captured=captured, raise_exc=RuntimeError("agent failure"))

        asyncio.run(_run_self_probe_cycle())  # must NOT propagate the exception

        assert captured, "agent_loop.run never called"
        assert not Path(captured[0]).exists(), (
            f"worktree not removed after agent exception: {captured[0]}"
        )

    def test_no_service_restart_call(self, tmp_path, monkeypatch):
        """_run_self_probe_cycle must never invoke systemctl restart/stop/start."""
        import asyncio
        repo_path, wt_root = _setup_probe_repo(tmp_path)
        monkeypatch.setattr(_proactive_mod, "_SELF_PROBE_RAWOS_REPO", repo_path)
        monkeypatch.setattr(_worktree_mod, "WORKTREE_ROOT", Path(wt_root))
        _mock_db(monkeypatch)
        _mock_agent(monkeypatch)

        restart_cmds: list[str] = []
        _real_run_bash = _proactive_mod.run_bash

        async def _spy(cmd: str, workdir: str = "", **kw):
            if any(tok in cmd for tok in ("systemctl restart", "systemctl stop",
                                           "systemctl start", "service rawos")):
                restart_cmds.append(cmd)
            return await _real_run_bash(cmd, workdir, **kw)

        monkeypatch.setattr(_proactive_mod, "run_bash", _spy)

        asyncio.run(_run_self_probe_cycle())

        assert not restart_cmds, f"service restart detected: {restart_cmds}"
