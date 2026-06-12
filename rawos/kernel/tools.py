"""
rawos Tool Registry — typed tool implementations for agent execution.
Each tool is an async function that returns a string result (tool output).
Tool definitions follow the OpenAI function-calling format for DeepSeek.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Awaitable

import httpx

from rawos.config import settings
from rawos.kernel.arch import get_arch
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

    whitelist = get_arch().shell_policy.readonly_whitelist()

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
        if base == "systemctl" and len(seg) >= 2 and seg[1] in whitelist.systemctl_subcmds:
            continue
        if base == "journalctl":
            if any(
                tok in whitelist.journalctl_blocked or tok.startswith("--vacuum")
                for tok in seg[1:]
            ):
                return False
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


async def _targets_rawos_own_repo(workdir: str) -> bool:
    """True if `workdir` is inside rawos's own live repo (/root/rawos).

    rawos.service runs with WorkingDirectory=/root/rawos and Restart=always.
    A `git checkout -b` or `git commit` whose repo root resolves to /root/rawos
    mutates the live runtime tree and can crash-loop the entity itself.
    """
    res: BashResult = await run_bash("git rev-parse --show-toplevel", workdir)
    return res.exit_code == 0 and res.stdout.strip() == settings.rawos_source_root


# ---------------------------------------------------------------------------
# Pass 2 — TIER enforcement helpers (self-modification of /root/rawos)
#
# Wired into execute() via the wrapper below (Pass 2 step b, commit 31864421).
# These git-introspection helpers detect and revert any tool call's side
# effect that touches a TIER 0 path inside rawos's own source tree.
# See PLAN.md "Phase 16 — Pass 2 — IMPLEMENTED (2026-06-09)".
# ---------------------------------------------------------------------------



_RAWOS_GIT_COMMON_DIR: str = settings.rawos_source_root + "/.git"

_TIER1_PREFIXES: tuple[str, ...] = (
    "tests/",
    "rawos/evaluation/",
    "rawos/dataset/",
    "rawos/study/",
    "rawos/timing/",
    "rawos/manifester/",
    "docs/",
)


async def _is_rawos_source_tree(workdir: str) -> bool:
    """True if `workdir` is rawos's own source tree — /root/rawos itself,
    or any git worktree linked to it.

    Linked worktrees report a different `--show-toplevel` (their own path)
    but share the SAME `--git-common-dir` as the main repo (/root/rawos/.git).
    A self-probe operating in an isolated worktree must still be subject to
    TIER enforcement, so this check (unlike `_targets_rawos_own_repo`, which
    is specifically about the live working tree's HEAD) follows common-dir,
    not toplevel.
    """
    res: BashResult = await run_bash(
        "git rev-parse --path-format=absolute --git-common-dir", workdir,
    )
    return res.exit_code == 0 and res.stdout.strip() == _RAWOS_GIT_COMMON_DIR


async def _git_status_porcelain(workdir: str) -> dict[str, str]:
    """Snapshot of `git status --porcelain=v1 -z -uall` as {path: "XY"}.

    Renames/copies are split into two synthetic entries: the new path keeps
    its real XY code, and the old path is recorded as "D " so a rename out
    of a TIER 1 directory is visible as a deletion at the old location.
    Returns {} if `workdir` is not inside a git repo.
    """
    res: BashResult = await run_bash("git status --porcelain=v1 -z -uall", workdir)
    if res.exit_code != 0:
        return {}

    entries: dict[str, str] = {}
    tokens = res.stdout.split("\0")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        code, path = tok[:2], tok[3:]
        entries[path] = code
        if code[0] in ("R", "C") and i + 1 < len(tokens):
            i += 1
            orig_path = tokens[i]
            if orig_path:
                entries.setdefault(orig_path, "D ")
        i += 1
    return entries


def _diff_paths(before: dict[str, str], after: dict[str, str]) -> set[str]:
    """Paths whose `_git_status_porcelain` entry changed between two snapshots.

    A path with the same status code in both snapshots is treated as
    untouched by the most recent tool call — even if it was already dirty.
    This is intentional, not a shortcut: files like `data/rawos.db` and
    `data/chroma/**` are continuously rewritten by the live service itself
    and are already permanently dirty in /root/rawos's working tree. If the
    wrapper instead diffed by content hash, it would (a) flag the live
    service's own writes as "tool violations" on every cycle, and (b) risk
    `git checkout`-reverting the live SQLite DB mid-write. Status-code
    equality is the safe, correct signal: "no NEW dirt appeared here".
    """
    paths = set(before) | set(after)
    return {p for p in paths if before.get(p) != after.get(p)}


def _in_tier1_allowlist(path: str) -> bool:
    """True if `path` (repo-relative) is under a TIER 1 directory.

    TIER 1 = tests/, rawos/evaluation/, rawos/dataset/, rawos/study/,
    rawos/timing/, rawos/manifester/, docs/ — see PLAN.md "THE HARD
    BOUNDARY". Everything else is TIER 0 (default-deny).

    This is the STATIC directory check only. Pass 2's execute() wrapper
    additionally enforces the bootstrap rule from PLAN.md Pass 1 item 2:
    a TIER 1 module's existing .py source files stay read-only until that
    module has its own passing tests (new test files are always writable
    under tests/).
    """
    return any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in _TIER1_PREFIXES)


async def _git_checkout_restore(workdir: str, path: str) -> BashResult:
    """Revert `path` to its HEAD state, undoing a TIER 0 violation.

    If `path` has no HEAD version (newly created file — status "??" or "A "),
    there is nothing to check out back to; instead it is unstaged and
    deleted entirely.
    """
    quoted = shlex.quote(path)
    head_has_path = await run_bash(f"git cat-file -e {shlex.quote('HEAD:' + path)}", workdir)
    if head_has_path.exit_code == 0:
        return await run_bash(f"git checkout HEAD -- {quoted}", workdir)

    await run_bash(f"git rm -f -r --cached -- {quoted}", workdir)
    return await run_bash(f"rm -rf -- {quoted}", workdir)


async def _git_branch(params: dict[str, Any], workdir: str) -> ToolResult:
    """Create and switch to a new rawos/* branch in the project repo.

    Enforces the rawos/* namespace so autonomous commits stay isolated.
    params.name: optional suffix (default: fix-{timestamp}).
    """
    if await _targets_rawos_own_repo(workdir):
        return ToolResult(
            output=(
                "error: refusing to create a branch inside /root/rawos — "
                "this is rawos's own live working tree (rawos.service runs "
                "from here with Restart=always). SIGNAL instead."
            ),
            success=False, duration_ms=0,
        )

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
    if await _targets_rawos_own_repo(workdir):
        return ToolResult(
            output=(
                "error: refusing to commit inside /root/rawos — "
                "this is rawos's own live working tree (rawos.service runs "
                "from here with Restart=always). SIGNAL instead."
            ),
            success=False, duration_ms=0,
        )

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


# ---------------------------------------------------------------------------
# Pass 2 step b — TIER enforcement wrapper around execute()
#
# Wires the helpers above into the single tool dispatch chokepoint. Two
# distinct rawos source-tree contexts are handled differently:
#
#   - LIVE (/root/rawos itself, rawos.service's WorkingDirectory,
#     Restart=always, continuously written by the running process): any
#     mutating tool is HARD-REFUSED. Detect-and-revert is unsafe here — it
#     could `git checkout`/`rm` a file the live service just wrote to
#     (data/rawos.db, data/chroma/**). SIGNAL only.
#
#   - WORKTREE (a linked git worktree of /root/rawos, e.g. an isolated
#     self-probe checkout — same --git-common-dir, different
#     --show-toplevel, no concurrent writer): mutating tools run normally,
#     then any change outside the TIER 1 allowlist (including changes
#     smuggled into a commit via `git reset --soft` + revert) is detected
#     and reverted.
#
# See PLAN.md "Phase 16 — Pass 2 — implementation design".
# ---------------------------------------------------------------------------

MUTATING_TOOLS: frozenset[str] = frozenset({"write_file", "bash", "git_branch", "git_commit"})

_TIER1_MODULE_DIRS: tuple[str, ...] = (
    "rawos/evaluation/",
    "rawos/dataset/",
    "rawos/study/",
    "rawos/timing/",
    "rawos/manifester/",
)


async def _is_bootstrap_blocked(path: str, workdir: str) -> bool:
    """True if `path` is an existing TIER 1 module's .py source file whose
    module has no dedicated tests yet (PLAN.md Pass 1 item 2 bootstrap rule:
    TIER 1 modules currently have ZERO test coverage, so editing their
    source is unverifiable until each module gets its own tests).

    New files are never bootstrap-blocked — only edits to pre-existing
    source files within a TIER 1 module directory. The caller is
    responsible for distinguishing new vs. pre-existing via the path's
    git status.
    """
    for module_dir in _TIER1_MODULE_DIRS:
        if path.startswith(module_dir) and path.endswith(".py"):
            module_name = module_dir.rstrip("/").rsplit("/", 1)[-1]
            res: BashResult = await run_bash(f"ls tests/test_{module_name}*.py", workdir)
            return not res.stdout.strip()
    return False


async def _tier_violations(workdir: str, before: dict[str, str], after: dict[str, str]) -> set[str]:
    """Paths changed between two `_git_status_porcelain` snapshots that
    violate the TIER boundary: not in `_in_tier1_allowlist`, or a
    bootstrap-blocked TIER 1 module source edit.
    """
    violations: set[str] = set()
    for path in _diff_paths(before, after):
        if not _in_tier1_allowlist(path):
            violations.add(path)
            continue
        status = after.get(path) or before.get(path) or ""
        is_new = status.startswith("?") or status.startswith("A")
        if not is_new and await _is_bootstrap_blocked(path, workdir):
            violations.add(path)
    return violations


async def _run_impl(impl: ToolFn, tool_name: str, params: dict[str, Any], workdir: str) -> ToolResult:
    try:
        return await impl(params, workdir)
    except Exception as e:
        log.exception("tool %s raised unexpectedly", tool_name)
        return ToolResult(output=f"tool error: {e}", success=False, duration_ms=0)


async def _execute_with_tier_enforcement(
    impl: ToolFn, tool_name: str, params: dict[str, Any], workdir: str,
) -> ToolResult:
    """Run a mutating tool inside a rawos source-tree worktree, then detect
    and revert any TIER 0 violation — including violations smuggled into a
    commit (undone via `git reset --soft` before the working-tree diff).
    """
    before_status = await _git_status_porcelain(workdir)
    before_head = (await run_bash("git rev-parse HEAD", workdir)).stdout.strip()

    result = await _run_impl(impl, tool_name, params, workdir)

    after_head = (await run_bash("git rev-parse HEAD", workdir)).stdout.strip()
    if before_head and after_head != before_head:
        reset_res = await run_bash(f"git reset --soft {before_head}", workdir)
        if reset_res.exit_code != 0:
            return ToolResult(
                output=(
                    result.output
                    + f"\n\nTIER ENFORCEMENT ERROR: git reset --soft {before_head} failed "
                    f"(exit {reset_res.exit_code}): {reset_res.stderr.strip()}. "
                    "Worktree may be in an inconsistent state — manual inspection required."
                ),
                success=False,
                duration_ms=result.duration_ms,
            )

    after_status = await _git_status_porcelain(workdir)
    violations = await _tier_violations(workdir, before_status, after_status)
    if not violations:
        return result

    for path in sorted(violations):
        await _git_checkout_restore(workdir, path)

    return ToolResult(
        output=(
            result.output
            + f"\n\nTIER VIOLATION: reverted {sorted(violations)} — outside the "
            "TIER 1 allowlist for rawos self-modification, or a TIER 1 module "
            "without its own test coverage yet (PLAN.md Pass 1 item 2)."
        ),
        success=False,
        duration_ms=result.duration_ms,
    )


async def execute(tool_name: str, params: dict[str, Any], workdir: str) -> ToolResult:
    """Execute a tool by name. Returns ToolResult; never raises.

    For tools in MUTATING_TOOLS operating inside rawos's own source tree
    (the live /root/rawos working tree, or any linked git worktree of it),
    this also enforces the Phase 16 TIER boundary — see PLAN.md "THE HARD
    BOUNDARY" and "Pass 2 — implementation design".
    """
    impl = REGISTRY.get(tool_name)
    if impl is None:
        return ToolResult(output=f"unknown tool: {tool_name}", success=False, duration_ms=0)

    if tool_name in MUTATING_TOOLS:
        if await _targets_rawos_own_repo(workdir):
            return ToolResult(
                output=(
                    f"error: refusing to run '{tool_name}' inside /root/rawos — "
                    "this is rawos's own live working tree (rawos.service runs "
                    "from here with Restart=always, and is continuously writing "
                    "data/rawos.db and data/chroma/**, so detect-and-revert is "
                    "unsafe here). Self-modification must happen in an isolated "
                    "git worktree. SIGNAL instead."
                ),
                success=False, duration_ms=0,
            )
        if await _is_rawos_source_tree(workdir):
            return await _execute_with_tier_enforcement(impl, tool_name, params, workdir)

    return await _run_impl(impl, tool_name, params, workdir)
