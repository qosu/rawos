"""
kernel/tools — _targets_rawos_own_repo + _is_rawos_source_tree use
Settings.rawos_source_root instead of hardcoded "/root/rawos".

Characterization:
- _targets_rawos_own_repo compares git show-toplevel against settings.rawos_source_root
- _is_rawos_source_tree compares git --git-common-dir against _RAWOS_GIT_COMMON_DIR,
  which is itself computed from settings.rawos_source_root + "/.git" at module import.

The tier enforcement tests patch _RAWOS_GIT_COMMON_DIR directly (a valid pattern
since it is a module-level variable, not a literal). Our tests use the same pattern
for _is_rawos_source_tree, and patch settings directly for _targets_rawos_own_repo.

Stage A: defaults are "/root/rawos" and "/root/rawos/.git" — identical to the
previous hardcoded literals. Zero behavior change.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from rawos.kernel.sandbox import BashResult
from rawos.kernel.tools import _is_rawos_source_tree, _targets_rawos_own_repo
import rawos.kernel.tools as tools


def _ok(stdout: str) -> BashResult:
    return BashResult(stdout=stdout, stderr="", exit_code=0, duration_ms=1, truncated=False)


def _fail() -> BashResult:
    return BashResult(stdout="", stderr="", exit_code=1, duration_ms=1, truncated=False)


def test_targets_rawos_own_repo_uses_settings_rawos_source_root():
    """Comparison uses settings.rawos_source_root, not hardcoded '/root/rawos'."""
    custom_root = "/custom/rawos-install"
    with patch("rawos.kernel.tools.settings") as mock_settings, \
         patch("rawos.kernel.tools.run_bash",
               new=AsyncMock(return_value=_ok(custom_root + "\n"))):
        mock_settings.rawos_source_root = custom_root
        result = asyncio.run(_targets_rawos_own_repo("/some/workdir"))
    assert result is True


def test_targets_rawos_own_repo_false_for_different_root():
    custom_root = "/custom/rawos-install"
    with patch("rawos.kernel.tools.settings") as mock_settings, \
         patch("rawos.kernel.tools.run_bash",
               new=AsyncMock(return_value=_ok("/other/path\n"))):
        mock_settings.rawos_source_root = custom_root
        result = asyncio.run(_targets_rawos_own_repo("/some/workdir"))
    assert result is False


def test_is_rawos_source_tree_uses_rawos_git_common_dir(monkeypatch):
    """_RAWOS_GIT_COMMON_DIR is computed from settings.rawos_source_root + '/.git';
    _is_rawos_source_tree uses this variable (same patching contract as tier
    enforcement tests — settable at runtime, not a buried literal)."""
    custom_git = "/custom/rawos-install/.git"
    monkeypatch.setattr(tools, "_RAWOS_GIT_COMMON_DIR", custom_git)

    with patch("rawos.kernel.tools.run_bash",
               new=AsyncMock(return_value=_ok(custom_git + "\n"))):
        result = asyncio.run(_is_rawos_source_tree("/some/worktree"))
    assert result is True


def test_is_rawos_source_tree_false_for_different_git_dir(monkeypatch):
    custom_git = "/custom/rawos-install/.git"
    monkeypatch.setattr(tools, "_RAWOS_GIT_COMMON_DIR", custom_git)

    with patch("rawos.kernel.tools.run_bash",
               new=AsyncMock(return_value=_ok("/other/repo/.git\n"))):
        result = asyncio.run(_is_rawos_source_tree("/some/worktree"))
    assert result is False


def test_rawos_git_common_dir_derived_from_settings_rawos_source_root():
    """Module-level _RAWOS_GIT_COMMON_DIR is settings.rawos_source_root + '/.git',
    proving it is no longer a hardcoded literal."""
    from rawos.config import settings
    assert tools._RAWOS_GIT_COMMON_DIR == settings.rawos_source_root + "/.git"
