"""
rawos Code Understanding Engine.
Reads actual changed code for STUCK and JUST_FINISHED triggers.
Uses direct subprocess calls — not the tool registry — to avoid import cycles.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("rawos.kernel.context_reader")


@dataclass(frozen=True)
class CodeContext:
    trigger_type: str
    repo_root: str
    changed_files: tuple[str, ...]
    unified_diff: str
    primary_file_content: str
    test_results: str


async def read_code_context(
    trigger_type: str,
    trigger_ctx: dict,
    workdir: str,
) -> CodeContext | None:
    if trigger_type not in ("STUCK", "JUST_FINISHED"):
        return None

    loop = asyncio.get_event_loop()

    diff_content = await loop.run_in_executor(None, _run_git_diff, workdir)

    changed_files: list[str] = []
    primary_file = ""

    if trigger_type == "STUCK":
        f = trigger_ctx.get("file", "")
        if f:
            changed_files = [f]
            primary_file = f

    elif trigger_type == "JUST_FINISHED":
        raw = trigger_ctx.get("files_changed", "")
        for line in raw.splitlines():
            # git diff --stat format: " path/to/file.py | 42 +++++++"
            # Skip summary line "N files changed..."
            if "|" in line:
                rel = line.split("|")[0].strip()
                if rel and not rel[0].isdigit():
                    abs_path = str(Path(workdir) / rel)
                    changed_files.append(abs_path)
                    if not primary_file:
                        primary_file = abs_path

    content = ""
    if primary_file:
        content = await loop.run_in_executor(None, _read_file_safe, primary_file)

    test_output = await loop.run_in_executor(None, _detect_and_run_tests, workdir)

    return CodeContext(
        trigger_type=trigger_type,
        repo_root=workdir,
        changed_files=tuple(changed_files),
        unified_diff=diff_content,
        primary_file_content=content,
        test_results=test_output,
    )


def _run_git_diff(workdir: str) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", workdir, "diff", "HEAD~1", "HEAD", "--unified=8"],
            capture_output=True, text=True, timeout=5.0,
        )
        return r.stdout[:3000] if r.returncode == 0 else ""
    except Exception:
        return ""


def _read_file_safe(path: str) -> str:
    try:
        p = Path(path)
        if not p.is_file():
            return ""
        if p.stat().st_size > 60_000:
            with p.open(errors="replace") as f:
                return f.read(4000) + "\n[... file truncated ...]"
        return p.read_text(errors="replace")[:4000]
    except OSError:
        return ""


def _detect_and_run_tests(workdir: str) -> str:
    """Run tests if a test runner config exists. Returns failure output only."""
    w = Path(workdir)
    # Ordered: most specific first
    markers: list[tuple[str, list[str]]] = [
        ("pytest.ini",     ["python3", "-m", "pytest", "-x", "--tb=line", "-q"]),
        ("setup.cfg",      ["python3", "-m", "pytest", "-x", "--tb=line", "-q"]),
        ("pyproject.toml", ["python3", "-m", "pytest", "-x", "--tb=line", "-q"]),
        ("Makefile",       ["make", "test"]),
    ]
    for marker, cmd in markers:
        if (w / marker).exists():
            try:
                r = subprocess.run(
                    cmd, cwd=workdir, capture_output=True, text=True, timeout=90.0,
                )
                combined = (r.stdout + r.stderr)[:2000]
                failure_lines = [
                    line for line in combined.splitlines()
                    if any(kw in line for kw in ("FAILED", "ERROR", "fail", "error", "assert"))
                ]
                return "\n".join(failure_lines) if failure_lines else "all tests passed"
            except subprocess.TimeoutExpired:
                return "tests timed out (90s)"
            except Exception:
                return ""
    return ""


def format_for_prompt(ctx: CodeContext) -> str:
    """Format CodeContext as a structured block appended to context_summary."""
    parts = ["", "--- CODE CONTEXT ---"]
    if ctx.changed_files:
        names = ", ".join(Path(f).name for f in ctx.changed_files[:5])
        parts.append("Changed files: " + names)
    if ctx.unified_diff:
        parts.append("\nActual diff:\n" + ctx.unified_diff)
    if ctx.primary_file_content:
        name = Path(ctx.changed_files[0]).name if ctx.changed_files else "file"
        parts.append(f"\n{name} content:\n{ctx.primary_file_content}")
    if ctx.test_results:
        parts.append(f"\nTest results: {ctx.test_results}")
    parts.append("---")
    return "\n".join(parts)
