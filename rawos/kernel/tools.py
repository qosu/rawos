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
from rawos.kernel import capability_gate as _capability_gate
from rawos.kernel import output_guard as _output_guard

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



# SHP.3 I-SEC8 — SSRF deny list: RFC1918 + loopback + link-local + IPv6 equivalents.
# _ssrf_blocked_url() must be called before every outbound HTTP request.
_SSRF_BLOCKED_NETWORKS: "list[ipaddress.IPv4Network | ipaddress.IPv6Network]" = []

def _build_ssrf_blocklist() -> None:
    """Populate _SSRF_BLOCKED_NETWORKS once at import time."""
    import ipaddress as _ip
    for cidr in (
        "10.0.0.0/8",       # RFC1918 private
        "172.16.0.0/12",    # RFC1918 private
        "192.168.0.0/16",   # RFC1918 private
        "127.0.0.0/8",      # loopback
        "169.254.0.0/16",   # link-local (cloud metadata: 169.254.169.254)
        "100.64.0.0/10",    # shared address space (RFC6598, carrier-grade NAT)
        "0.0.0.0/8",        # "this" network
        "::1/128",           # IPv6 loopback
        "fc00::/7",          # IPv6 unique local
        "fe80::/10",         # IPv6 link-local
        "64:ff9b::/96",     # IPv4-mapped IPv6
    ):
        _SSRF_BLOCKED_NETWORKS.append(_ip.ip_network(cidr, strict=False))

_build_ssrf_blocklist()


def _ssrf_blocked_url(url: str) -> str | None:
    """Return a human-readable reason if url should be blocked for SSRF; None if allowed.

    Resolves hostname → checks all resolved IPs. Fail-closed: resolution error = blocked.
    Also rejects non-http(s) schemes (redundant with caller check, defence-in-depth).
    """
    import ipaddress as _ip
    import socket as _socket
    from urllib.parse import urlparse as _urlparse

    parsed = _urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"SSRF-blocked: scheme {parsed.scheme!r} not allowed (only http/https)"

    hostname = parsed.hostname
    if not hostname:
        return "SSRF-blocked: empty hostname"

    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    # Resolve hostname → list of (family, type, proto, canonname, sockaddr)
    try:
        infos = _socket.getaddrinfo(hostname, port, proto=_socket.IPPROTO_TCP)
    except _socket.gaierror as e:
        return f"SSRF-blocked: hostname resolution failed: {e}"

    if not infos:
        return "SSRF-blocked: hostname resolved to no addresses"

    for _fam, _type, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            addr = _ip.ip_address(ip_str)
        except ValueError:
            return f"SSRF-blocked: could not parse resolved IP: {ip_str!r}"
        for net in _SSRF_BLOCKED_NETWORKS:
            if addr in net:
                return (
                    f"SSRF-blocked: {hostname!r} resolves to {ip_str} "
                    f"which is in denied range {net}"
                )

    return None  # allowed


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

    # I-SEC8: SSRF guard — deny RFC1918/loopback/link-local (fail-closed)
    ssrf_reason = _ssrf_blocked_url(url)
    if ssrf_reason:
        return ToolResult(output=f"error: {ssrf_reason}", success=False, duration_ms=0)

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



async def _manage_file(params: dict[str, Any], workdir: str) -> ToolResult:
    """Managed file edits (R1 operator path): allowlist mgmt + propose/apply lifecycle."""
    import time as _time
    import rawos.db as _db
    from rawos.kernel.billing_context import get_billing_context
    from rawos.kernel.arch.base import FileOperatorRefusalError
    from rawos.kernel.operator import OperatorError, operate_on_file, execute_approved_file_edit

    ctx = get_billing_context()
    if ctx is None:
        return ToolResult(output="manage_file: no active agent context", success=False, duration_ms=0)
    user_id: str = ctx["user_id"]

    action = params.get("action", "")
    target_path: str = params.get("target_path", "")
    t0 = _time.monotonic()

    def _ms() -> int:
        return int((_time.monotonic() - t0) * 1000)

    if action == "add_target":
        validator_cmd: str = params.get("validator_cmd", "")
        if not target_path or not validator_cmd:
            return ToolResult(
                output="manage_file add_target: target_path and validator_cmd required",
                success=False, duration_ms=_ms(),
            )
        _db.add_managed_file_target(user_id, target_path, validator_cmd)
        return ToolResult(
            output=f"registered: {target_path} (validator: {validator_cmd})",
            success=True, duration_ms=_ms(),
        )

    if action == "remove_target":
        if not target_path:
            return ToolResult(
                output="manage_file remove_target: target_path required",
                success=False, duration_ms=_ms(),
            )
        _db.remove_managed_file_target(user_id, target_path)
        return ToolResult(output=f"removed: {target_path}", success=True, duration_ms=_ms())

    if action == "status":
        if not target_path:
            return ToolResult(
                output="manage_file status: target_path required",
                success=False, duration_ms=_ms(),
            )
        managed = _db.get_managed_file_target(user_id, target_path)
        if managed is None:
            return ToolResult(
                output=f"not allowlisted: {target_path}",
                success=True, duration_ms=_ms(),
            )
        track = _db.get_operator_track_record(user_id, "file_edit", target_path)
        output = (
            f"target: {target_path}\n"
            f"validator_cmd: {managed['validator_cmd']}\n"
            f"graduated: {track.graduated}\n"
            f"verified_successes: {track.verified_successes}\n"
            f"last_outcome: {track.last_outcome}"
        )
        return ToolResult(output=output, success=True, duration_ms=_ms())

    if action == "edit":
        if not target_path:
            return ToolResult(
                output="manage_file edit: target_path required",
                success=False, duration_ms=_ms(),
            )
        new_content = params.get("new_content", "").encode("utf-8")
        try:
            outcome = operate_on_file(user_id, target_path, new_content)
        except FileOperatorRefusalError as exc:
            return ToolResult(output=f"refused (self-protection): {exc}", success=False, duration_ms=_ms())
        except OperatorError as exc:
            return ToolResult(output=f"operator error: {exc}", success=False, duration_ms=_ms())

        if outcome.auto_applied:
            res = outcome.operation_result
            assert res is not None
            status = "auto-applied" if res.verified else "applied-but-validator-failed (restored)"
            return ToolResult(
                output=f"{status}: {target_path}\nreason: {outcome.reason}",
                success=res.verified, duration_ms=_ms(),
            )
        return ToolResult(
            output=(
                f"proposed (pending owner approval): {target_path}\n"
                f"reason: {outcome.reason}\n"
                "Call manage_file(action=\'approved_apply\', ...) after reviewing to apply."
            ),
            success=True, duration_ms=_ms(),
        )

    if action == "approved_apply":
        if not target_path:
            return ToolResult(
                output="manage_file approved_apply: target_path required",
                success=False, duration_ms=_ms(),
            )
        new_content = params.get("new_content", "").encode("utf-8")
        try:
            res = execute_approved_file_edit(user_id, target_path, new_content)
        except FileOperatorRefusalError as exc:
            return ToolResult(output=f"refused (self-protection): {exc}", success=False, duration_ms=_ms())
        except OperatorError as exc:
            return ToolResult(output=f"operator error: {exc}", success=False, duration_ms=_ms())
        status = "applied and verified" if res.verified else "applied but validator failed (restored)"
        return ToolResult(output=f"{status}: {target_path}", success=res.verified, duration_ms=_ms())

    return ToolResult(
        output=(
            f"manage_file: unknown action {action!r}. "
            "Valid: add_target, remove_target, status, edit, approved_apply"
        ),
        success=False, duration_ms=_ms(),
    )

async def _manage_service(params: dict[str, Any], workdir: str) -> ToolResult:
    """Managed service lifecycle (R2 operator path): allowlist mgmt + propose/apply lifecycle."""
    import time as _time
    import rawos.db as _db
    from rawos.kernel.billing_context import get_billing_context
    from rawos.kernel.operator import (
        OperatorError,
        ServiceOperatorRefusalError,
        operate_on_service,
        execute_approved_service_action,
    )

    ctx = get_billing_context()
    if ctx is None:
        return ToolResult(output="manage_service: no active agent context", success=False, duration_ms=0)
    user_id: str = ctx["user_id"]

    action = params.get("action", "")
    service_name: str = params.get("service_name", "")
    t0 = _time.monotonic()

    def _ms() -> int:
        return int((_time.monotonic() - t0) * 1000)

    if action == "add_target":
        validator_cmd: str = params.get("validator_cmd", "")
        if not service_name or not validator_cmd:
            return ToolResult(
                output="manage_service add_target: service_name and validator_cmd required",
                success=False, duration_ms=_ms(),
            )
        _db.add_managed_service_target(user_id, service_name, validator_cmd)
        return ToolResult(
            output=f"registered: {service_name} (validator: {validator_cmd})",
            success=True, duration_ms=_ms(),
        )

    if action == "remove_target":
        if not service_name:
            return ToolResult(
                output="manage_service remove_target: service_name required",
                success=False, duration_ms=_ms(),
            )
        _db.remove_managed_service_target(user_id, service_name)
        return ToolResult(output=f"removed: {service_name}", success=True, duration_ms=_ms())

    if action == "status":
        if not service_name:
            return ToolResult(
                output="manage_service status: service_name required",
                success=False, duration_ms=_ms(),
            )
        managed = _db.get_managed_service_target(user_id, service_name)
        if managed is None:
            return ToolResult(
                output=f"not allowlisted: {service_name}",
                success=True, duration_ms=_ms(),
            )
        results = []
        for op_action in ("restart", "start", "stop"):
            op_class = f"service_{op_action}"
            track = _db.get_operator_track_record(user_id, op_class, service_name)
            results.append(
                f"  {op_action}: graduated={track.graduated} "
                f"verified_successes={track.verified_successes} "
                f"last_outcome={track.last_outcome}"
            )
        output = (
            f"target: {service_name}\n"
            f"validator_cmd: {managed['validator_cmd']}\n"
            "graduation:\n" + "\n".join(results)
        )
        return ToolResult(output=output, success=True, duration_ms=_ms())

    if action == "action":
        svc_action: str = params.get("svc_action", "")
        if not service_name or not svc_action:
            return ToolResult(
                output="manage_service action: service_name and svc_action required",
                success=False, duration_ms=_ms(),
            )
        try:
            outcome = operate_on_service(user_id, service_name, svc_action)
        except ServiceOperatorRefusalError as exc:
            return ToolResult(output=f"refused (self-protection): {exc}", success=False, duration_ms=_ms())
        except OperatorError as exc:
            return ToolResult(output=f"operator error: {exc}", success=False, duration_ms=_ms())

        if outcome.auto_applied:
            res = outcome.operation_result
            assert res is not None
            status = "auto-applied" if res.verified else "applied-but-validator-failed (restored)"
            return ToolResult(
                output=f"{status}: {svc_action} {service_name}\nreason: {outcome.reason}",
                success=res.verified, duration_ms=_ms(),
            )
        return ToolResult(
            output=(
                f"proposed (pending owner approval): {svc_action} {service_name}\n"
                f"reason: {outcome.reason}\n"
                "Call manage_service(action='approved_apply', ...) after reviewing to apply."
            ),
            success=True, duration_ms=_ms(),
        )

    if action == "approved_apply":
        svc_action = params.get("svc_action", "")
        if not service_name or not svc_action:
            return ToolResult(
                output="manage_service approved_apply: service_name and svc_action required",
                success=False, duration_ms=_ms(),
            )
        try:
            res = execute_approved_service_action(user_id, service_name, svc_action)
        except ServiceOperatorRefusalError as exc:
            return ToolResult(output=f"refused (self-protection): {exc}", success=False, duration_ms=_ms())
        except OperatorError as exc:
            return ToolResult(output=f"operator error: {exc}", success=False, duration_ms=_ms())
        status = "applied and verified" if res.verified else "applied but validator failed (restored)"
        return ToolResult(
            output=f"{status}: {svc_action} {service_name}",
            success=res.verified, duration_ms=_ms(),
        )

    return ToolResult(
        output=(
            f"manage_service: unknown action {action!r}. "
            "Valid: add_target, remove_target, status, action, approved_apply"
        ),
        success=False, duration_ms=_ms(),
    )



async def _manage_pam(params: dict[str, Any], workdir: str) -> ToolResult:
    """PAM target allowlist + owner-approved PAM write (R3-adjacent, no autonomous path)."""
    import time as _time
    from pathlib import Path as _Path
    import rawos.db as _db
    from rawos.kernel.billing_context import get_billing_context
    from rawos.kernel.pam_operator import (
        PamRefusalError,
        PamInstallError,
        commit_pam_edit,
        execute_approved_pam_edit,
        _SELF_PROTECTED_PAM_FILES,
    )
    from rawos.kernel.operator import OperatorError

    ctx = get_billing_context()
    if ctx is None:
        return ToolResult(output="manage_pam: no active agent context", success=False, duration_ms=0)
    user_id: str = ctx["user_id"]

    action = params.get("action", "")
    pam_file: str = params.get("pam_file", "")
    t0 = _time.monotonic()

    def _ms() -> int:
        return int((_time.monotonic() - t0) * 1000)

    # Injectable test overrides (not in public TOOL_DEFINITIONS schema)
    _pam_dir = _Path(params["_test_pam_dir"]) if "_test_pam_dir" in params else None
    _backup_dir = _Path(params["_test_backup_dir"]) if "_test_backup_dir" in params else None
    _systemd = params.get("_test_systemd")
    _raw_probe = params.get("_test_probe_fn")
    _probe_fn = (lambda: bool(_raw_probe)) if _raw_probe is not None else None

    if action == "add_target":
        if not pam_file:
            return ToolResult(
                output="manage_pam add_target: pam_file required",
                success=False, duration_ms=_ms(),
            )
        _db.add_managed_pam_target(user_id, pam_file)
        protected = pam_file in _SELF_PROTECTED_PAM_FILES
        warning = " (WARNING: self-protected — approved_apply will refuse)" if protected else ""
        return ToolResult(
            output=f"registered: {pam_file}{warning}",
            success=True, duration_ms=_ms(),
        )

    if action == "remove_target":
        if not pam_file:
            return ToolResult(
                output="manage_pam remove_target: pam_file required",
                success=False, duration_ms=_ms(),
            )
        _db.remove_managed_pam_target(user_id, pam_file)
        return ToolResult(output=f"removed: {pam_file}", success=True, duration_ms=_ms())

    if action == "status":
        if not pam_file:
            return ToolResult(
                output="manage_pam status: pam_file required",
                success=False, duration_ms=_ms(),
            )
        if pam_file in _SELF_PROTECTED_PAM_FILES:
            return ToolResult(
                output=f"{pam_file}: self-protected (in _SELF_PROTECTED_PAM_FILES — refused at construction)",
                success=True, duration_ms=_ms(),
            )
        managed = _db.get_managed_pam_target(user_id, pam_file)
        if managed is None:
            return ToolResult(
                output=f"not allowlisted: {pam_file}",
                success=True, duration_ms=_ms(),
            )
        return ToolResult(
            output=f"target: {pam_file}\nstatus: allowlisted (protected=no, oracle=probe-key)",
            success=True, duration_ms=_ms(),
        )

    if action == "approved_apply":
        new_content: str = params.get("new_content", "")
        if not pam_file or not new_content:
            return ToolResult(
                output="manage_pam approved_apply: pam_file and new_content required",
                success=False, duration_ms=_ms(),
            )
        try:
            snap_id = execute_approved_pam_edit(
                user_id, pam_file, new_content.encode(),
                _systemd=_systemd,
                _probe_fn=_probe_fn,
                _pam_dir=_pam_dir,
                _backup_dir=_backup_dir,
            )
        except PamRefusalError as exc:
            return ToolResult(output=f"refused (self-protection floor): {exc}", success=False, duration_ms=_ms())
        except PamInstallError as exc:
            return ToolResult(output=f"install failed (probe or apply error): {exc}", success=False, duration_ms=_ms())
        except OperatorError as exc:
            return ToolResult(output=f"operator error: {exc}", success=False, duration_ms=_ms())
        return ToolResult(
            output=(
                f"applied and ARMED: {pam_file}\n"
                f"snapshot_id: {snap_id}\n"
                "Deadman timer running. Verify auth in a NEW session, then call\n"
                "manage_pam(action='commit') to disarm — or wait for auto-revert."
            ),
            success=True, duration_ms=_ms(),
        )

    if action == "commit":
        commit_pam_edit(_systemd=_systemd)
        return ToolResult(
            output="rawos-pam-revert deadman disarmed. PAM change committed.",
            success=True, duration_ms=_ms(),
        )

    return ToolResult(
        output=(
            f"manage_pam: unknown action {action!r}. "
            "Valid: add_target, remove_target, status, approved_apply, commit"
        ),
        success=False, duration_ms=_ms(),
    )


async def _manage_owned_resource(params: dict, workdir: str) -> "ToolResult":
    """M3 R-own: manage owned-resource lifecycle (workspace GC, DB vacuum).

    action="gc"             -- propose or auto-apply workspace GC for target_path
    action="approved_apply" -- owner path: bypass gate, apply immediately
    action="status"         -- show graduation + recent history
    action="restore"        -- restore a trashed workspace from trash_path
    action="reap"           -- hard-delete trash older than retention window
    """
    import time as _time
    ctx = _get_agent_context()
    if ctx is None:
        return ToolResult(output="manage_owned_resource: no active agent context", success=False, duration_ms=0)
    user_id: str = ctx["user_id"]

    action = params.get("action", "")
    target_path: str = params.get("target_path", "")
    t0 = _time.monotonic()

    def _ms() -> int:
        return int((_time.monotonic() - t0) * 1000)

    from rawos.kernel.owned_resource import (
        OwnedOpSpec,
        OwnedResourceRefusalError,
        get_default_kernel,
    )
    import rawos.db as _db

    kernel = get_default_kernel()

    if action == "gc":
        if not target_path:
            return ToolResult(
                output="manage_owned_resource gc: target_path required",
                success=False, duration_ms=_ms(),
            )
        active_dirs = frozenset(_db.get_active_workspace_dirs())
        trash_root = params.get("trash_root") or None
        spec = OwnedOpSpec(op_type="workspace_gc", target_path=target_path, trash_root=trash_root)
        try:
            outcome = kernel.operate_on_owned_resource(
                user_id=user_id, op_spec=spec, active_workspace_dirs=active_dirs
            )
        except OwnedResourceRefusalError as exc:
            return ToolResult(output=f"refused (ownership floor): {exc}", success=False, duration_ms=_ms())
        if outcome.auto_applied:
            return ToolResult(
                output=f"auto-applied: trashed {target_path}\n  trash: {outcome.trash_path}",
                success=True, duration_ms=_ms(),
            )
        return ToolResult(
            output=(
                f"proposed (not applied): {target_path}\n"
                f"reason: {outcome.reason}\n"
                "Call manage_owned_resource(action='approved_apply', ...) after review."
            ),
            success=True, duration_ms=_ms(),
        )

    if action == "approved_apply":
        if not target_path:
            return ToolResult(
                output="manage_owned_resource approved_apply: target_path required",
                success=False, duration_ms=_ms(),
            )
        active_dirs = frozenset(_db.get_active_workspace_dirs())
        trash_root = params.get("trash_root") or None
        spec = OwnedOpSpec(op_type="workspace_gc", target_path=target_path, trash_root=trash_root)
        try:
            result = kernel.execute_approved_owned_op(
                user_id=user_id, op_spec=spec, active_workspace_dirs=active_dirs
            )
        except OwnedResourceRefusalError as exc:
            return ToolResult(output=f"refused (ownership floor): {exc}", success=False, duration_ms=_ms())
        return ToolResult(
            output=f"applied: trashed {target_path}\n  trash: {result.trash_path}",
            success=True, duration_ms=_ms(),
        )

    if action == "restore":
        trash_path: str = params.get("trash_path", "")
        if not trash_path:
            return ToolResult(
                output="manage_owned_resource restore: trash_path required",
                success=False, duration_ms=_ms(),
            )
        try:
            kernel.restore_from_trash(trash_path)
        except (FileNotFoundError, FileExistsError, OwnedResourceRefusalError) as exc:
            return ToolResult(output=f"restore failed: {exc}", success=False, duration_ms=_ms())
        return ToolResult(output=f"restored from trash: {trash_path}", success=True, duration_ms=_ms())

    if action == "reap":
        from rawos.config import settings as _s
        trash_root = params.get("trash_root") or str(
            __import__("pathlib").Path(_s.rawos_source_root) / "data" / ".trash"
        )
        retention_days = int(params.get("retention_days", _s.owned_trash_retention_days))
        reaped = kernel.reap_trash(trash_root=trash_root, retention_days=retention_days)
        return ToolResult(
            output=f"reaped {len(reaped)} trash entries (older than {retention_days}d)",
            success=True, duration_ms=_ms(),
        )

    if action == "status":
        from rawos.config import settings as _s
        history = _db.list_owned_resource_history(limit=5)
        history_lines = [
            f"  {r['op_type']} {r['outcome']} auto={bool(r['autonomous'])} @ {r['created_at']}"
            for r in history
        ] or ["  (no history)"]
        track_ws = _db.get_operator_track_record(user_id, "owned_workspace_gc", _s.workspaces_root)
        track_db = _db.get_operator_track_record(user_id, "owned_db_vacuum", _s.rawos_source_root)
        output = (
            f"operator_owned_enabled: {_s.operator_owned_enabled}\n"
            f"workspace_gc:  graduated={track_ws.graduated} verified={track_ws.verified_successes}\n"
            f"db_vacuum:     graduated={track_db.graduated} verified={track_db.verified_successes}\n"
            "recent history (newest first):\n"
            + "\n".join(history_lines)
        )
        return ToolResult(output=output, success=True, duration_ms=_ms())

    return ToolResult(
        output=(
            f"manage_owned_resource: unknown action {action!r}. "
            "Valid: gc, approved_apply, restore, reap, status"
        ),
        success=False, duration_ms=_ms(),
    )



async def _manage_venv(params: dict, workdir: str) -> "ToolResult":
    """M3 Stage 2 R-venv: reversible dependency operator.

    action="propose"         -- stage candidate + run preflight; report hash delta
    action="approved_apply"  -- owner path: bypass gate, preflight + arm_and_swap
    action="status"          -- show graduation + recent venv history
    """
    import time as _time
    ctx = _get_agent_context()
    if ctx is None:
        return ToolResult(output="manage_venv: no active agent context", success=False, duration_ms=0)
    user_id: str = ctx["user_id"]

    action = params.get("action", "")
    t0 = _time.monotonic()

    def _ms() -> int:
        return int((_time.monotonic() - t0) * 1000)

    from rawos.kernel.venv_operator import (
        VenvDepSpec,
        VenvPreflightError,
        VenvStateError,
        execute_approved_venv_op,
        operate_on_venv,
    )
    import rawos.db as _db
    from rawos.config import settings as _s

    requirements: list[str] = params.get("requirements", [])
    if isinstance(requirements, str):
        requirements = [r.strip() for r in requirements.split(",") if r.strip()]

    if action == "propose":
        dep_spec = VenvDepSpec(requirements=requirements)
        try:
            outcome = operate_on_venv(user_id, dep_spec)
        except (VenvPreflightError, VenvStateError) as exc:
            return ToolResult(output=f"venv propose failed: {exc}", success=False, duration_ms=_ms())
        if outcome.auto_applied:
            return ToolResult(
                output=f"auto-applied: venv swapped to candidate (service restarting)",
                success=True, duration_ms=_ms(),
            )
        return ToolResult(
            output=(
                f"proposed (not applied): {outcome.reason}\n"
                "Use manage_venv(action='approved_apply', ...) after review."
            ),
            success=True, duration_ms=_ms(),
        )

    if action == "approved_apply":
        dep_spec = VenvDepSpec(requirements=requirements)
        try:
            outcome = execute_approved_venv_op(user_id, dep_spec)
        except VenvPreflightError as exc:
            return ToolResult(output=f"preflight failed: {exc}", success=False, duration_ms=_ms())
        except VenvStateError as exc:
            return ToolResult(output=f"single-flight conflict: {exc}", success=False, duration_ms=_ms())
        return ToolResult(
            output=f"applied: venv swapped to candidate (service restarting)",
            success=True, duration_ms=_ms(),
        )

    if action == "status":
        history = _db.list_venv_op_history(limit=5)
        history_lines = [
            f"  {r['op_type']} {r['outcome']} auto={bool(r['autonomous'])} @ {r['created_at']}"
            for r in history
        ] or ["  (no history)"]
        track_ws = _db.get_operator_track_record(user_id, "venv_dep_update", _s.rawos_source_root)
        output = (
            f"operator_venv_enabled: {_s.operator_venv_enabled}\n"
            f"venv_dep_update: graduated={track_ws.graduated} verified={track_ws.verified_successes}\n"
            "recent history (newest first):\n"
            + "\n".join(history_lines)
        )
        return ToolResult(output=output, success=True, duration_ms=_ms())

    return ToolResult(
        output=(
            f"manage_venv: unknown action {action!r}. "
            "Valid: propose, approved_apply, status"
        ),
        success=False, duration_ms=_ms(),
    )


REGISTRY: dict[str, ToolFn] = {
    "bash":           _bash,
    "bash_readonly":  _bash_readonly,
    "write_file":     _write_file,
    "read_file":      _read_file,
    "list_files":     _list_files,
    "fetch_url":      _fetch_url,
    "deploy":         _deploy,
    "git_branch":     _git_branch,
    "git_commit":     _git_commit,
    "manage_file":    _manage_file,
    "manage_service": _manage_service,
    "manage_pam":     _manage_pam,
    "manage_owned_resource": _manage_owned_resource,
    "manage_venv":           _manage_venv,
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
    {
        "type": "function",
        "function": {
            "name": "manage_file",
            "description": (
                "Manage host files via the R1 reversible operator path. "
                "Actions: add_target (register path+validator in allowlist), "
                "remove_target (deregister), status (show allowlist+graduation state), "
                "edit (propose or auto-apply based on graduation), "
                "approved_apply (execute owner-approved edit, always records toward graduation). "
                "Requires files to be pre-allowlisted with a validator_cmd. "
                "Never touches rawos service files or source tree."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add_target", "remove_target", "status", "edit", "approved_apply"],
                        "description": "Operation to perform",
                    },
                    "target_path": {
                        "type": "string",
                        "description": "Absolute path to the managed file",
                    },
                    "validator_cmd": {
                        "type": "string",
                        "description": "Shell command that exits 0 iff the file is valid (required for add_target)",
                    },
                    "new_content": {
                        "type": "string",
                        "description": "New UTF-8 file content (required for edit and approved_apply)",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_service",
            "description": (
                "Manage host service lifecycle via the R2 reversible operator path. "
                "Actions: add_target (register service_name+validator in allowlist), "
                "remove_target (deregister), status (show allowlist+graduation state per action), "
                "action (propose or auto-apply restart/start/stop based on graduation), "
                "approved_apply (execute owner-approved action, always records toward graduation). "
                "Requires services to be pre-allowlisted with a validator_cmd. "
                "Never operates on rawos.service or ssh/sshd (self-protection floor)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add_target", "remove_target", "status", "action", "approved_apply"],
                        "description": "Operation to perform",
                    },
                    "service_name": {
                        "type": "string",
                        "description": "systemd service unit name (e.g. caddy.service)",
                    },
                    "svc_action": {
                        "type": "string",
                        "enum": ["restart", "start", "stop"],
                        "description": "Service lifecycle action (required for action and approved_apply)",
                    },
                    "validator_cmd": {
                        "type": "string",
                        "description": "Shell command that exits 0 iff the service is healthy (required for add_target)",
                    },
                },
                "required": ["action"],
            },
        },
    },
            {
            "type": "function",
            "function": {
                "name": "manage_pam",
                "description": (
                    "Manage host PAM write authority via the R3-adjacent owner-approved path. "
                    "Actions: add_target (allowlist a non-root-critical pam.d file), "
                    "remove_target (deregister), status (show allowlist + protection status), "
                    "approved_apply (execute owner-approved PAM edit — arms deadman, writes pam.d, "
                    "runs live-auth probe; returns snapshot_id in ARMED state), "
                    "commit (disarm deadman after out-of-band verification). "
                    "Never writes sshd/common-auth/sudo/login or any self-protected file. "
                    "No autonomous path — approved_apply requires explicit owner call each time."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["add_target", "remove_target", "status", "approved_apply", "commit"],
                            "description": "Operation to perform",
                        },
                        "pam_file": {
                            "type": "string",
                            "description": "pam.d filename without path prefix (e.g. rawos-guest)",
                        },
                        "new_content": {
                            "type": "string",
                            "description": "Full new pam.d file content (required for approved_apply)",
                        },
                    },
                    "required": ["action"],
                },
            },
        },
    {
        "type": "function",
        "function": {
            "name": "manage_owned_resource",
            "description": (
                "M3 R-own: lifecycle management over rawos owned namespace "
                "(workspaces, data artefacts). Boundary-enforced: cannot reach "
                "system-level paths outside owned roots (I-OWN1). "
                "Reversible: deletion = move-to-trash, restorable until retention window. "
                "actions: gc (propose/auto-apply workspace GC), "
                "approved_apply (owner path, bypass gate), "
                "restore (restore from trash), reap (hard-delete old trash), "
                "status (show graduation + history)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "One of: gc, approved_apply, restore, reap, status",
                    },
                    "target_path": {
                        "type": "string",
                        "description": (
                            "Absolute path to the workspace dir to GC or apply. "
                            "Must be inside an owned root (workspaces_root or data/). "
                            "Required for gc and approved_apply."
                        ),
                    },
                    "trash_root": {
                        "type": "string",
                        "description": "Override trash root dir. Default: rawos_source_root/data/.trash",
                    },
                    "trash_path": {
                        "type": "string",
                        "description": "Trash entry path for restore action.",
                    },
                    "retention_days": {
                        "type": "integer",
                        "description": "Override retention window for reap action.",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_venv",
            "description": (
                "M3 Stage 2 R-venv: reversible Python dependency operator. "
                "Builds a candidate venv, proves it (import rawos.api.app + smoke tests), "
                "then swaps via rename with a deadman timer for auto-revert on no-boot. "
                "DORMANT by default (operator_venv_enabled=False) — propose-only until owner activates. "
                "actions: propose (gate-gated), approved_apply (owner path), status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "One of: propose, approved_apply, status",
                    },
                    "requirements": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "pip-installable specs, e.g. ['requests==2.31', 'httpx>=0.26']. "
                            "Required for propose and approved_apply."
                        ),
                    },
                },
                "required": ["action"],
            },
        },
    }]


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
        result = await impl(params, workdir)
        # SHP.4 I-SEC7: scan output for secrets before returning to agent loop
        from rawos.kernel.billing_context import get_billing_context as _get_bc
        _ctx = _get_bc()
        _uid = _ctx["user_id"] if _ctx else "<unknown>"
        guarded = _output_guard.guard_output(result.output, tool_name, _uid)
        if guarded is not result.output:
            result = ToolResult(output=guarded, success=result.success, duration_ms=result.duration_ms)
        return result
    except Exception as e:
        log.exception("tool %s raised unexpectedly", tool_name)
        return ToolResult(output=f"tool error: {e}", success=False, duration_ms=0)


async def _execute_with_tier_enforcement(
    impl: ToolFn, tool_name: str, params: dict[str, Any], workdir: str,
) -> ToolResult:
    """Run a mutating tool inside a rawos source-tree worktree, then detect
    and revert any TIER 0 violation — including violations smuggled into a
    commit (undone via `git reset --soft` before the working-tree diff).

    For linked worktrees (workdir != main rawos repo), the enforcement also
    snapshots the live main repo before and after the tool call.  This closes
    escape vectors that bypass the worktree-local git status:
      - absolute-path writes to /root/rawos/**
      - writes through in-worktree symlinks pointing to live TIER 0 files
      - writes via hardlinks sharing an inode with a live TIER 0 file
    Any change detected in the live repo's TIER 0 paths is reverted via
    `git checkout HEAD` in that repo and reported as a TIER VIOLATION.
    """
    from pathlib import Path as _Path

    # Detect linked-worktree mode: workdir is a linked worktree, not the main repo.
    # In that mode we additionally monitor the live rawos repo for out-of-worktree writes.
    _main_repo = str(_Path(_RAWOS_GIT_COMMON_DIR).parent)
    _is_linked = _Path(workdir).resolve() != _Path(_main_repo).resolve()

    before_status = await _git_status_porcelain(workdir)
    before_live = await _git_status_porcelain(_main_repo) if _is_linked else {}
    before_head = (await run_bash("git rev-parse HEAD", workdir)).stdout.strip()

    result = await _run_impl(impl, tool_name, params, workdir)

    after_head = (await run_bash("git rev-parse HEAD", workdir)).stdout.strip()
    if before_head and after_head != before_head:
        diff_res = await run_bash(
            f"git diff --name-only {before_head} {after_head}", workdir
        )
        committed_paths = (
            set(diff_res.stdout.strip().splitlines())
            if diff_res.exit_code == 0
            else set()
        )
        commit_has_tier0_path = diff_res.exit_code != 0 or any(
            not _in_tier1_allowlist(p) for p in committed_paths if p
        )
        if commit_has_tier0_path:
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

    # Check live repo for out-of-worktree writes (linked-worktree mode only).
    if _is_linked:
        after_live = await _git_status_porcelain(_main_repo)
        live_violations = await _tier_violations(_main_repo, before_live, after_live)
        if live_violations:
            for path in sorted(live_violations):
                await _git_checkout_restore(_main_repo, path)
            return ToolResult(
                output=(
                    result.output
                    + f"\n\nTIER VIOLATION (LIVE REPO): out-of-worktree write detected and "
                    f"reverted in {sorted(live_violations)} — worktree tools must not modify "
                    "the live rawos repo directly (absolute paths, symlinks, or hardlinks)."
                ),
                success=False,
                duration_ms=result.duration_ms,
            )

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
    # SHP.4 I-SEC6: capability gate — classify tier + audit (audit-first SHP.4)
    from rawos.kernel.billing_context import get_billing_context as _get_bc
    _ectx = _get_bc()
    _euid = _ectx["user_id"] if _ectx else "<unknown>"
    _gate = _capability_gate.pre_execute_gate(tool_name, params, workdir, _euid)
    if not _gate.allowed:
        return ToolResult(output=f"error: {_gate.reason}", success=False, duration_ms=0)

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
