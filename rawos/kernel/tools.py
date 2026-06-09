"""
rawos Tool Registry — typed tool implementations for agent execution.
Each tool is an async function that returns a string result (tool output).
Tool definitions follow the OpenAI function-calling format for DeepSeek.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Awaitable

import httpx

from rawos.config import settings
from rawos.kernel.sandbox import BashResult, PathTraversalError, run_bash, run_bash_in_container, validate_path

log = logging.getLogger("rawos.tools")

_FETCH_TIMEOUT  = 15   # seconds
_FETCH_MAX_SIZE = 200_000  # bytes


@dataclass(frozen=True)
class ToolResult:
    output:      str
    success:     bool
    duration_ms: int


ToolFn = Callable[[dict[str, Any], str], Awaitable[ToolResult]]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
# Destructive command guard — enforced regardless of sandbox mode

_DESTRUCTIVE_PATTERNS: tuple[str, ...] = (
    "rm -rf /",
    "rm -rf ~",
    "rm -rf $HOME",
    "rm -f /etc",
    "rm -f /usr",
    "rm -f /root",
    "> /dev/sda",
    "> /dev/nvme",
    "dd if=",
    "mkfs",
    "fdisk /dev",
    "parted /dev",
    ":(){ :|:& };:",
    "shutdown",
    "reboot",
    "init 0",
    "init 6",
    "systemctl stop rawos",
    "iptables -F",
)


def _is_destructive(cmd: str) -> bool:
    """Return True if command matches a known-destructive pattern."""
    low = cmd.lower().replace(" ", "")
    return any(p.replace(" ", "") in low for p in _DESTRUCTIVE_PATTERNS)


# ---------------------------------------------------------------------------

async def _bash(params: dict[str, Any], workdir: str) -> ToolResult:
    import time
    command = params.get("command", "").strip()
    if not command:
        return ToolResult(output="error: command is required", success=False, duration_ms=0)
    if _is_destructive(command):
        return ToolResult(output="blocked: destructive command pattern", success=False, duration_ms=0)

    result: BashResult = (
        await run_bash_in_container(command, workdir)
        if settings.sandbox_docker
        else await run_bash(command, workdir)
    )

    parts = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(f"[stderr]\n{result.stderr}")
    if result.exit_code != 0:
        parts.append(f"[exit code: {result.exit_code}]")
    if result.truncated:
        parts.append("[output was truncated]")

    output = "\n".join(parts) if parts else "(no output)"
    return ToolResult(
        output=output,
        success=(result.exit_code == 0),
        duration_ms=result.duration_ms,
    )


_BASH_READONLY_CMDS: frozenset[str] = frozenset({
    "cat", "grep", "egrep", "fgrep", "ls", "find", "head", "tail",
    "wc", "diff", "echo", "pwd", "which", "file", "stat", "sort", "uniq", "cut",
})
_BASH_READONLY_GIT_SUBCMDS: frozenset[str] = frozenset({
    "log", "diff", "show", "status", "branch", "tag", "stash",
    "ls-files", "ls-tree", "rev-parse", "describe", "shortlog", "blame",
})

# Shell metacharacters that enable command chaining or subshell injection.
# Checked against the raw command string before any parsing.
_INJECTION_MARKERS: tuple[str, ...] = (";", "&&", "||", "`", "$(", "${")

# Shell tokens that write output to files (space-separated redirect forms).
_WRITE_REDIRECT_TOKENS: frozenset[str] = frozenset({">", ">>"})


def _is_bash_readonly_safe(command: str) -> bool:
    """Return True iff command (including all pipe segments) is read-only safe.

    Guards:
    - Command chaining / injection: ; && || ` $( ${
    - File write redirection: > >> (space-separated forms)
    - Any pipe segment whose base command is not on the whitelist
    - git invocations with non-read-only subcommands

    Handles both space-separated (> file) and no-space (>file, >>file) forms.
    """
    import shlex as _shlex
    from pathlib import Path as _Path

    cmd = command.strip()
    if not cmd:
        return False

    # Reject command chaining and subshell injection before any parsing
    for marker in _INJECTION_MARKERS:
        if marker in cmd:
            return False

    try:
        all_tokens = _shlex.split(cmd)
    except ValueError:
        return False
    if not all_tokens:
        return False

    # Reject file write redirection tokens — both space-separated (> file) and
    # no-space forms (>file, >>file) that shlex parses as a single token.
    for tok in all_tokens:
        if tok in _WRITE_REDIRECT_TOKENS or tok.startswith(">"):
            return False

    # Split on | to validate every pipe segment independently
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in all_tokens:
        if tok == "|":
            if current:
                segments.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    if not segments:
        return False

    for seg in segments:
        if not seg:
            return False
        base = _Path(seg[0]).name
        if base in _BASH_READONLY_CMDS:
            continue
        if base == "git" and len(seg) >= 2 and seg[1] in _BASH_READONLY_GIT_SUBCMDS:
            continue
        return False

    return True


async def _bash_readonly(params: dict[str, Any], workdir: str) -> ToolResult:
    """Read-only shell: strict whitelist, no writes, no network."""
    command = params.get("command", "").strip()
    if not command:
        return ToolResult(output="error: command is required", success=False, duration_ms=0)
    if not _is_bash_readonly_safe(command):
        return ToolResult(
            output=f"error: command not in read-only whitelist — rejected: {command!r}",
            success=False,
            duration_ms=0,
        )
    result: BashResult = await run_bash(command, workdir)
    parts = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(f"[stderr]\n{result.stderr}")
    if result.exit_code != 0:
        parts.append(f"[exit code: {result.exit_code}]")
    if result.truncated:
        parts.append("[output was truncated]")
    output = "\n".join(parts) if parts else "(no output)"
    return ToolResult(output=output, success=(result.exit_code == 0), duration_ms=result.duration_ms)


async def _write_file(params: dict[str, Any], workdir: str) -> ToolResult:
    import time
    start = time.monotonic()
    path_str = params.get("path", "").strip()
    content  = params.get("content", "")

    if not path_str:
        return ToolResult(output="error: path is required", success=False, duration_ms=0)

    try:
        full_path = validate_path(path_str, workdir)
    except PathTraversalError as e:
        return ToolResult(output=f"error: {e}", success=False, duration_ms=0)

    try:
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        size = full_path.stat().st_size
        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolResult(
            output=f"wrote {size} bytes to {path_str}",
            success=True,
            duration_ms=duration_ms,
        )
    except OSError as e:
        return ToolResult(output=f"error writing file: {e}", success=False, duration_ms=0)


async def _read_file(params: dict[str, Any], workdir: str) -> ToolResult:
    import time
    start = time.monotonic()
    path_str = params.get("path", "").strip()

    if not path_str:
        return ToolResult(output="error: path is required", success=False, duration_ms=0)

    try:
        full_path = validate_path(path_str, workdir)
    except PathTraversalError as e:
        return ToolResult(output=f"error: {e}", success=False, duration_ms=0)

    if not full_path.exists():
        return ToolResult(output=f"error: file not found: {path_str}", success=False, duration_ms=0)

    try:
        content = full_path.read_text(encoding="utf-8", errors="replace")
        if len(content) > 50_000:
            content = content[:50_000] + "\n[file truncated — read first 50 000 chars]";
        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolResult(output=content, success=True, duration_ms=duration_ms)
    except OSError as e:
        return ToolResult(output=f"error reading file: {e}", success=False, duration_ms=0)


async def _list_files(params: dict[str, Any], workdir: str) -> ToolResult:
    import time
    start = time.monotonic()
    path_str = params.get("path", ".").strip() or "."

    try:
        full_path = validate_path(path_str, workdir)
    except PathTraversalError as e:
        return ToolResult(output=f"error: {e}", success=False, duration_ms=0)

    if not full_path.exists():
        return ToolResult(output=f"error: path not found: {path_str}", success=False, duration_ms=0)

    if not full_path.is_dir():
        return ToolResult(output=f"error: not a directory: {path_str}", success=False, duration_ms=0)

    try:
        entries = sorted(full_path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        lines = []
        for entry in entries[:200]:   # limit listing to 200 entries
            rel = str(entry.relative_to(Path(workdir).resolve()))
            if entry.is_dir():
                lines.append(f"d  {rel}/")
            else:
                size = entry.stat().st_size
                lines.append(f"f  {rel}  ({size} bytes)")

        if not lines:
            lines = ["(empty directory)"]

        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolResult(output="\n".join(lines), success=True, duration_ms=duration_ms)
    except OSError as e:
        return ToolResult(output=f"error listing files: {e}", success=False, duration_ms=0)


async def _fetch_url(params: dict[str, Any], workdir: str) -> ToolResult:
    import time
    start = time.monotonic()
    url       = params.get("url", "").strip()
    max_chars = int(params.get("max_chars", 5000))
    max_chars = max(100, min(max_chars, 20_000))

    if not url:
        return ToolResult(output="error: url is required", success=False, duration_ms=0)
    if not url.startswith(("http://", "https://")):
        return ToolResult(output="error: url must start with http:// or https://", success=False, duration_ms=0)

    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=_FETCH_TIMEOUT,
            headers={"User-Agent": "rawos/0.2 (+https://downgrade.app)"},
        ) as client:
            resp = await client.get(url)
            content_type = resp.headers.get("content-type", "")
            raw = resp.content[:_FETCH_MAX_SIZE]

            if "text" in content_type or "json" in content_type:
                text = raw.decode("utf-8", errors="replace")
            else:
                text = f"[binary content, {len(raw)} bytes, type: {content_type}]"

            if len(text) > max_chars:
                text = text[:max_chars] + f"\n[truncated — fetched {len(text)} chars total]"

            duration_ms = int((time.monotonic() - start) * 1000)
            return ToolResult(
                output=f"HTTP {resp.status_code}\n{text}",
                success=(200 <= resp.status_code < 300),
                duration_ms=duration_ms,
            )
    except httpx.TimeoutException:
        return ToolResult(output=f"error: request timed out ({_FETCH_TIMEOUT}s)", success=False, duration_ms=0)
    except httpx.RequestError as e:
        return ToolResult(output=f"error: {e}", success=False, duration_ms=0)



async def _deploy(params: dict[str, Any], workdir: str) -> ToolResult:
    t0 = time.monotonic()
    entry_point = str(params.get("entry_point", "index.html"))
    # Sanitise: no path traversal, no absolute paths
    entry_clean = Path(entry_point).name
    if not entry_clean:
        return ToolResult(output="error: invalid entry_point", success=False, duration_ms=0)

    entry_path = Path(workdir) / entry_clean
    if not entry_path.is_file():
        # Auto-detect: try index.html if requested entry not found
        alt = Path(workdir) / "index.html"
        if alt.is_file():
            entry_clean = "index.html"
        else:
            return ToolResult(
                output="error: no deployable entry point found (expected index.html)",
                success=False,
                duration_ms=int((time.monotonic() - t0) * 1000),
            )

    project_id = Path(workdir).name
    url = f"https://downgrade.app/preview/{project_id}/{entry_clean}"
    return ToolResult(
        output=url,
        success=True,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )

# ---------------------------------------------------------------------------
# Registry and OpenAI-format definitions
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Git tools — Level 2+ only.  Enforces rawos/* branch namespace.
# ---------------------------------------------------------------------------

_PROTECTED_BRANCHES: frozenset[str] = frozenset({"main", "master", "develop", "HEAD"})


async def _git_branch(params: dict[str, Any], workdir: str) -> ToolResult:
    """Create and switch to a new rawos/* branch in the project repo.

    Enforces the rawos/* namespace so autonomous commits stay isolated.
    params.name: optional suffix (default: fix-{timestamp}).
    """
    import time as _time
    import re   as _re

    raw_name = (params.get("name") or "").strip()
    if raw_name:
        suffix = raw_name[len("rawos/"):] if raw_name.startswith("rawos/") else raw_name
    else:
        suffix = f"fix-{int(_time.time())}"

    if not _re.fullmatch(r"[a-zA-Z0-9_\-\.]+", suffix):
        return ToolResult(
            output=(
                f"error: invalid branch suffix '{suffix}' — "
                "only alphanumeric, dash, underscore, dot are allowed"
            ),
            success=False, duration_ms=0,
        )

    if suffix in _PROTECTED_BRANCHES:
        return ToolResult(
            output=(
                f"error: refusing to create rawos/{suffix} — "
                f"'{suffix}' is a protected branch name"
            ),
            success=False, duration_ms=0,
        )

    branch_name = f"rawos/{suffix}"
    result: BashResult = await run_bash(f"git checkout -b {branch_name}", workdir)
    parts = []
    if result.stdout:
        parts.append(result.stdout)
    if result.stderr:
        parts.append(result.stderr)
    return ToolResult(
        output="\n".join(parts).strip() or f"Created and switched to branch {branch_name}",
        success=(result.exit_code == 0),
        duration_ms=result.duration_ms,
    )


async def _git_commit(params: dict[str, Any], workdir: str) -> ToolResult:
    """Stage all changes and commit to the current branch.

    Hard gate: refuses to commit if not on a rawos/* branch.
    Uses rawos author identity so every autonomous commit is attributable.
    Returns the full git commit output (includes branch name and short hash).
    """
    import shlex as _shlex

    message = (params.get("message") or "rawos: autonomous fix").strip() or "rawos: autonomous fix"

    # --- safety gate: verify we are on a rawos/* branch -------------------
    branch_res: BashResult = await run_bash("git rev-parse --abbrev-ref HEAD", workdir)
    if branch_res.exit_code != 0:
        return ToolResult(
            output="error: not a git repository or repo has no commits yet",
            success=False, duration_ms=0,
        )
    current_branch = branch_res.stdout.strip()
    if not current_branch.startswith("rawos/"):
        return ToolResult(
            output=(
                f"error: refusing to commit to '{current_branch}' — "
                "rawos only commits to rawos/* branches. "
                "Call git_branch first to create an isolated rawos/* branch."
            ),
            success=False, duration_ms=0,
        )

    # --- stage all changes ------------------------------------------------
    add_res: BashResult = await run_bash("git add -A", workdir)
    if add_res.exit_code != 0:
        return ToolResult(
            output=f"error: git add -A failed: {add_res.stderr.strip()}",
            success=False, duration_ms=add_res.duration_ms,
        )

    # --- abort if nothing staged ------------------------------------------
    staged_res: BashResult = await run_bash("git diff --cached --name-only", workdir)
    if not staged_res.stdout.strip():
        return ToolResult(
            output="nothing to commit — no staged changes after git add -A",
            success=False, duration_ms=0,
        )

    # --- commit with rawos identity ---------------------------------------
    safe_msg = _shlex.quote(message)
    commit_res: BashResult = await run_bash(
        f'git -c user.name="rawos" -c user.email="rawos@autonomous.local" commit -m {safe_msg}',
        workdir,
    )
    parts = []
    if commit_res.stdout:
        parts.append(commit_res.stdout)
    if commit_res.stderr:
        parts.append(commit_res.stderr)
    return ToolResult(
        output="\n".join(parts).strip(),
        success=(commit_res.exit_code == 0),
        duration_ms=commit_res.duration_ms,
    )


REGISTRY: dict[str, ToolFn] = {
    "bash":       _bash,
    "bash_readonly": _bash_readonly,
    "write_file":  _write_file,
    "read_file":   _read_file,
    "list_files":  _list_files,
    "fetch_url":   _fetch_url,
    "deploy":      _deploy,
    "git_branch":  _git_branch,
    "git_commit":  _git_commit,
}

TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Execute a shell command in the project workspace. "
                "Use for running scripts, compiling, checking output. "
                "Workspace is isolated; 30s timeout; 50KB output limit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_readonly",
            "description": (
                "Execute a read-only shell command in the project workspace. "
                "Allowed: cat, grep, ls, find, head, tail, wc, diff, echo, pwd, which, file, stat, "
                "git log/diff/show/status/branch/tag/stash/ls-files/ls-tree/rev-parse/describe. "
                "Rejects all write/network operations. Use this for all inspection tasks."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Read-only shell command to run"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file in the project workspace with given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string", "description": "File path relative to workspace root"},
                    "content": {"type": "string", "description": "Complete file content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the project workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to workspace root"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories in the project workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to workspace (default: root)", "default": "."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch content from a URL (HTML, JSON, text). Use for real-time info or reference.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url":       {"type": "string", "description": "URL to fetch"},
                    "max_chars": {"type": "integer", "description": "Max characters to return (default 5000, max 20000)"},
                },
                "required": ["url"],
            },
        },
    },    {
        "type": "function",
        "function": {
            "name": "deploy",
            "description": (
                "Publish the project workspace to a public URL. "
                "Use after building a website or app to make it accessible in the browser. "
                "Returns the public URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entry_point": {"type": "string", "description": "Entry file to serve (default: index.html)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_branch",
            "description": (
                "Create a new rawos/* branch in the project repo and switch to it. "
                "Branch name is automatically prefixed with rawos/. "
                "Always call this BEFORE git_commit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Branch suffix (rawos/ prefix added automatically). Default: fix-{timestamp}.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": (
                "Stage ALL changes (git add -A) and commit to the current rawos/* branch. "
                "REFUSES if not on a rawos/* branch — call git_branch first. "
                "Returns the commit output including branch name and short hash."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Commit message (default: rawos: autonomous fix).",
                    },
                },
            },
        },
    },
]


async def execute(tool_name: str, params: dict[str, Any], workdir: str) -> ToolResult:
    """Execute a tool by name. Returns ToolResult; never raises."""
    impl = REGISTRY.get(tool_name)
    if impl is None:
        return ToolResult(output=f"unknown tool: {tool_name}", success=False, duration_ms=0)
    try:
        return await impl(params, workdir)
    except Exception as e:
        log.exception("tool %s raised unexpectedly", tool_name)
        return ToolResult(output=f"tool error: {e}", success=False, duration_ms=0)
