"""
rawos execution sandbox — subprocess isolation for tool execution.
Security boundary: cwd=workdir, path validation, timeout, output size limit.
Note: Docker-level isolation deferred to Phase 5 hardening.
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path

from rawos.kernel.arch import get_arch


_OUTPUT_LIMIT = 50_000   # bytes max stdout+stderr captured
_TIMEOUT      = 30       # seconds


class SandboxError(Exception):
    pass


class PathTraversalError(SandboxError):
    pass


@dataclass(frozen=True)
class BashResult:
    stdout:       str
    stderr:       str
    exit_code:    int
    duration_ms:  int
    truncated:    bool


def validate_path(path: str, workdir: str) -> Path:
    """
    Resolve path relative to workdir. Raise PathTraversalError if it escapes workdir.
    Handles both absolute and relative paths from AI tool calls.
    """
    workdir_abs = Path(workdir).resolve()
    if os.path.isabs(path):
        candidate = Path(path).resolve()
    else:
        candidate = (workdir_abs / path).resolve()

    try:
        candidate.relative_to(workdir_abs)
    except ValueError:
        raise PathTraversalError(f"path escapes workspace: {path}")

    return candidate


async def run_bash(command: str, workdir: str) -> BashResult:
    """
    Execute shell command in workdir with resource limits.
    Returns BashResult; never raises on non-zero exit.
    """
    workdir_abs = str(Path(workdir).resolve())

    # Wrap with resource limits via the arch backend's ShellPolicy
    wrapped, exec_kwargs = get_arch().shell_policy.wrap(command, workdir_abs)

    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_shell(
            wrapped,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir_abs,
            **exec_kwargs,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return BashResult(
                stdout="", stderr="Command timed out (30s limit)",
                exit_code=124, duration_ms=_TIMEOUT * 1000, truncated=False,
            )
    except Exception as e:
        raise SandboxError(f"failed to launch subprocess: {e}") from e

    duration_ms = int((time.monotonic() - start) * 1000)

    # Decode and enforce output size limit
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    combined_len = len(stdout) + len(stderr)
    truncated = False
    if combined_len > _OUTPUT_LIMIT:
        # Trim stdout first, then stderr
        keep = _OUTPUT_LIMIT
        if len(stdout) > keep:
            stdout = stdout[:keep] + "\n[output truncated]"
            stderr = ""
        else:
            stderr = stderr[:max(0, keep - len(stdout))] + "\n[output truncated]"
        truncated = True

    return BashResult(
        stdout=stdout, stderr=stderr,
        exit_code=proc.returncode or 0,
        duration_ms=duration_ms,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Docker container sandbox (production path)
# ---------------------------------------------------------------------------

DOCKER_IMAGE    = "python:3.12-slim"
_CONTAINER_MEM  = "512m"
_CONTAINER_CPUS = "0.5"
_CONTAINER_PIDS = "64"


async def run_bash_in_container(command: str, workdir: str) -> BashResult:
    """
    Execute shell command inside an ephemeral Docker container.
    Security guarantees:
      - No network access (--network none)
      - 512MB RAM, 0.5 CPU, 64 process limit
      - Cannot gain new privileges (--security-opt no-new-privileges)
      - Only project workdir is visible (/workspace bind-mount)
      - Container destroyed after each call (--rm)
    """
    abs_workdir = str(Path(workdir).resolve())

    docker_cmd = [
        "docker", "run", "--rm",
        "--network", "none",
        "--memory", _CONTAINER_MEM,
        "--cpus", _CONTAINER_CPUS,
        "--pids-limit", _CONTAINER_PIDS,
        "--ulimit", "nofile=1024:1024",
        "--security-opt", "no-new-privileges",
        "-v", f"{abs_workdir}:/workspace",
        "-w", "/workspace",
        DOCKER_IMAGE,
        "bash", "-c", command,
    ]

    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *docker_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return BashResult(
                stdout="",
                stderr=f"Command timed out ({_TIMEOUT}s limit)",
                exit_code=124,
                duration_ms=_TIMEOUT * 1000,
                truncated=False,
            )
    except FileNotFoundError:
        raise SandboxError("docker executable not found — cannot run sandboxed bash")
    except Exception as e:
        raise SandboxError(f"container execution failed: {e}") from e

    duration_ms = int((time.monotonic() - start) * 1000)

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    combined_len = len(stdout) + len(stderr)
    truncated = False
    if combined_len > _OUTPUT_LIMIT:
        keep = _OUTPUT_LIMIT
        if len(stdout) > keep:
            stdout = stdout[:keep] + "\n[output truncated]"
            stderr = ""
        else:
            stderr = stderr[:max(0, keep - len(stdout))] + "\n[output truncated]"
        truncated = True

    return BashResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=proc.returncode or 0,
        duration_ms=duration_ms,
        truncated=truncated,
    )
