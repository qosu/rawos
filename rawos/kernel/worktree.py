"""
rawos worktree manager — disposable git worktrees for autonomous repo investigation.

SERVER_SCAN runs operate on repos rawos does not own and whose live working
trees are continuously used by other processes (e.g. research-foundry.timer
checks out branches in /root/liveproof-agent's main working copy). All
autonomous fix-investigation work for such repos happens in a disposable
worktree under _WORKTREE_ROOT — never the live tree — so rawos can never
collide with whatever that repo's own automation is doing.

A worktree is just an additional checkout sharing the same object database
and refs as the origin repo: branches and commits created inside it
(via the git_branch/git_commit tools) remain visible from the origin repo
after the worktree is removed.
"""
from __future__ import annotations

import logging
import shutil
import time
import uuid
from pathlib import Path

from rawos.config import settings
from rawos.kernel.sandbox import SandboxError, run_bash

log = logging.getLogger("rawos.kernel.worktree")

WORKTREE_ROOT = Path(settings.worktree_root)


async def create_worktree(repo_path: str) -> str | None:
    """Create a disposable detached-HEAD worktree of repo_path.

    Detached HEAD avoids "branch already checked out" errors when the live
    tree has some branch (possibly a stale rawos/* branch) checked out —
    git_branch creates a fresh rawos/* branch from this detached HEAD.

    Returns the absolute path to the new worktree, or None if repo_path is
    not a git repo or worktree creation failed (caller should fall back to
    read-only diagnostics in-place).
    """
    repo = Path(repo_path).resolve()
    if not (repo / ".git").exists():
        log.info("worktree: %s is not a git repo root — skipping isolation", repo)
        return None

    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    name = f"{repo.name}-{int(time.time())}-{uuid.uuid4().hex[:8]}"
    target = WORKTREE_ROOT / name

    result = await run_bash(f"git worktree add --detach '{target}' HEAD", str(repo))
    if result.exit_code != 0:
        log.warning("worktree: git worktree add failed for %s: %s", repo, result.stderr[:300])
        return None

    return str(target)


async def get_head_sha(worktree_path: str) -> str | None:
    """Return the commit SHA at worktree_path's current HEAD, or None on error.

    Called immediately after create_worktree() succeeds, while the worktree
    is still at its initial detached HEAD (the commit the anomaly was
    detected against) — before the agent makes any commits. Used as
    anomaly_verifier.verify_fix()'s base_ref for the pre-fix test run.
    """
    try:
        result = await run_bash("git rev-parse HEAD", worktree_path)
    except SandboxError:
        return None
    if result.exit_code != 0:
        return None
    return result.stdout.strip()


async def remove_worktree(worktree_path: str) -> None:
    """Remove a disposable worktree.

    Branches/commits created inside it are preserved in the origin repo —
    `git worktree remove` only deletes the checkout directory and the
    worktree's entry in the shared .git/worktrees metadata, never refs.
    """
    p = Path(worktree_path)
    if not p.exists():
        return
    if not str(p.resolve()).startswith(str(WORKTREE_ROOT.resolve())):
        log.error("worktree: refusing to remove path outside %s: %s", WORKTREE_ROOT, p)
        return

    result = await run_bash("git rev-parse --git-common-dir", str(p))
    if result.exit_code == 0:
        common_dir = Path(result.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = (p / common_dir).resolve()
        repo_root = common_dir.parent if common_dir.name == ".git" else common_dir
        rm = await run_bash(f"git worktree remove --force '{p}'", str(repo_root))
        if rm.exit_code == 0:
            return
        log.warning("worktree: git worktree remove failed for %s: %s", p, rm.stderr[:300])

    # Fallback: best-effort filesystem cleanup so disposable worktrees never accumulate.
    shutil.rmtree(p, ignore_errors=True)
