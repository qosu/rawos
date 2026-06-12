"""
kernel/sandbox.run_bash — wired to kernel/arch ShellPolicy ABI.

Characterization: run_bash must call get_arch().shell_policy.wrap(command,
workdir_abs) to build the wrapped shell command, then pass the returned
shell_cmd to asyncio.create_subprocess_shell (merging returned exec_kwargs
into the subprocess call) — instead of inlining the cd/ulimit prefix.
Stage A is a zero-behavior-change extraction — this test is the proof.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from rawos.kernel.sandbox import run_bash


def _mock_arch(wrap_return):
    backend = MagicMock()
    backend.shell_policy.wrap.return_value = wrap_return
    return backend


def test_run_bash_calls_shell_policy_wrap_with_workdir_abs(tmp_path):
    workdir_abs = str(Path(tmp_path).resolve())
    backend = _mock_arch(("echo wrapped-command", {}))

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"out", b""))
    fake_proc.returncode = 0

    with patch("rawos.kernel.sandbox.get_arch", return_value=backend), \
         patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=fake_proc)) as mock_shell:
        result = asyncio.run(run_bash("echo hi", str(tmp_path)))

    backend.shell_policy.wrap.assert_called_once_with("echo hi", workdir_abs)
    mock_shell.assert_called_once()
    call_args, call_kwargs = mock_shell.call_args
    assert call_args[0] == "echo wrapped-command"
    assert call_kwargs["cwd"] == workdir_abs
    assert result.stdout == "out"


def test_run_bash_merges_exec_kwargs_from_shell_policy(tmp_path):
    backend = _mock_arch(("echo wrapped", {"env": {"FOO": "bar"}}))

    fake_proc = MagicMock()
    fake_proc.communicate = AsyncMock(return_value=(b"", b""))
    fake_proc.returncode = 0

    with patch("rawos.kernel.sandbox.get_arch", return_value=backend), \
         patch("asyncio.create_subprocess_shell", new=AsyncMock(return_value=fake_proc)) as mock_shell:
        asyncio.run(run_bash("echo hi", str(tmp_path)))

    _, call_kwargs = mock_shell.call_args
    assert call_kwargs["env"] == {"FOO": "bar"}
