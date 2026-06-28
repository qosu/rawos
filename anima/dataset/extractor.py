"""
anima Dataset Extractor — Phase 8.

Extracts labeled examples from tg-claude sessions.db.
Each session contributes at most one example: the most substantive user
message becomes true_goal; behavioral_context is reconstructed from
the workdir filesystem state.

Silver labels: domain classification is heuristic. Quality is inherently
lower than synthetic examples. These anchor the dataset in real user intent.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

from anima.dataset.schema import BehavioralContext, DatasetExample

log = logging.getLogger("anima.dataset.extractor")

_TG_CLAUDE_DB = "/root/tg-claude/sessions.db"

# Workdir basename hints → domain (checked before keyword scan)
_WORKDIR_HINTS: dict[str, str] = {
    "research": "research",
    "paper": "research",
    "science": "research",
    "deepseek": "research",
    "deepseekngu": "research",
    "training": "research",
    "bot": "deployment",
    "superbot": "deployment",
    "tg": "deployment",
    "telegram": "deployment",
    "app": "feature",
    "web": "feature",
    "site": "feature",
    "extension": "ui",
    "extensionn": "ui",
    "email": "api",
    "emailtracking": "api",
    "image": "feature",
    "imagerevi": "feature",
    "love": "feature",
    "loveaz": "feature",
    "downgrade": "deployment",
    "downgradeapp": "deployment",
    "monk": "feature",
    "selfimpro": "research",
}

# English keyword → domain (checked in message text)
_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "debugging":   ["bug", "error", "fix", "debug", "trace", "exception", "crash", "fail"],
    "feature":     ["add", "implement", "build", "create", "new", "feature", "endpoint"],
    "refactor":    ["refactor", "clean", "rename", "move", "extract", "restructure"],
    "auth":        ["auth", "login", "token", "jwt", "session", "password", "oauth"],
    "data":        ["database", "schema", "migration", "query", "sql", "model", "table"],
    "api":         ["api", "endpoint", "route", "rest", "graphql", "request", "response"],
    "ui":          ["ui", "frontend", "component", "page", "style", "css", "layout"],
    "performance": ["slow", "performance", "optimize", "cache", "latency", "speed"],
    "testing":     ["test", "spec", "mock", "assert", "pytest", "coverage"],
    "deployment":  ["deploy", "docker", "nginx", "server", "production", "service"],
    "research":    ["research", "paper", "doi", "zenodo", "publish", "arxiv", "thesis"],
}

_NON_INTENT_PREFIXES = (
    "nãy tôi và bạn",
    "bạn nhớ",
    "tôi với bạn đang",
    "chúng ta đang làm",
    "alo",
    "hello",
    "hi ",
    "hey ",
    "nãy tôi với bạn",
)


def _is_substantive_intent(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 20:
        return False
    lower = stripped.lower()
    for prefix in _NON_INTENT_PREFIXES:
        if lower.startswith(prefix):
            return False
    return True


def _classify_domain(workdir: str, message: str) -> str:
    basename = Path(workdir).name.lower()

    # 1. Workdir hint (fast path)
    for hint, domain in _WORKDIR_HINTS.items():
        if hint in basename:
            return domain

    # 2. English keywords in message
    lower = message.lower()
    scores: dict[str, int] = {}
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        count = sum(lower.count(kw) for kw in keywords)
        if count > 0:
            scores[domain] = count
    if scores:
        return max(scores, key=scores.__getitem__)

    return "general"


def _infer_stack(workdir: str) -> list[str]:
    from anima.context.user_model import _EXT_STACK
    path = Path(workdir)
    if not path.exists():
        return []
    stack: list[str] = []
    try:
        for f in path.rglob("*"):
            if not f.is_file():
                continue
            parts = str(f).split("/")
            if any(skip in parts for skip in (".git", "__pycache__", "node_modules", ".venv", "venv")):
                continue
            tag = _EXT_STACK.get(f.suffix)
            if tag and tag not in stack:
                stack.append(tag)
            if len(stack) >= 5:
                break
    except PermissionError:
        pass
    return stack


def _list_recent_files(workdir: str, limit: int = 8) -> list[str]:
    path = Path(workdir)
    if not path.exists():
        return []
    files: list[tuple[float, str]] = []
    try:
        for f in path.rglob("*"):
            if not f.is_file():
                continue
            parts = str(f).split("/")
            if any(skip in parts for skip in (".git", "__pycache__", "node_modules", ".venv", "venv")):
                continue
            try:
                mtime = f.stat().st_mtime
                rel = str(f.relative_to(path))
                files.append((mtime, rel))
            except (OSError, ValueError):
                continue
    except PermissionError:
        pass
    files.sort(reverse=True)
    return [rel for _, rel in files[:limit]]


def _pick_best_message(messages: list[dict]) -> str | None:
    user_msgs = [
        m.get("content", "")
        for m in messages
        if m.get("role") == "user"
    ]
    # Normalize content: handle list-of-parts format
    texts: list[str] = []
    for raw in user_msgs:
        if isinstance(raw, list):
            text = " ".join(
                part.get("text", "") for part in raw if isinstance(part, dict)
            ).strip()
        else:
            text = str(raw).strip()
        if _is_substantive_intent(text):
            texts.append(text)

    if not texts:
        return None
    # Pick the most informative: longest but capped at 300 chars to avoid multi-turn rambling
    return max(texts, key=lambda t: min(len(t), 300))


def extract_from_tg_claude(db_path: str = _TG_CLAUDE_DB) -> list[DatasetExample]:
    """
    Extract one DatasetExample per tg-claude session.
    Returns only validated examples (invalid ones are logged and skipped).
    """
    examples: list[DatasetExample] = []
    seen_goals: set[str] = set()

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT user_id, workdir, messages, updated FROM sessions ORDER BY updated DESC"
        ).fetchall()
        conn.close()
    except Exception as exc:
        log.error("failed to open tg-claude db %s: %s", db_path, exc)
        return []

    for row in rows:
        workdir = row["workdir"] or "/root"
        try:
            messages = json.loads(row["messages"] or "[]")
        except json.JSONDecodeError:
            log.warning("skipping session workdir=%s — messages JSON invalid", workdir)
            continue

        best_msg = _pick_best_message(messages)
        if not best_msg:
            log.debug("skipping session workdir=%s — no substantive user message", workdir)
            continue

        # Deduplicate on normalized goal text
        goal_key = best_msg[:100].lower()
        if goal_key in seen_goals:
            log.debug("skipping duplicate goal: %s", goal_key[:60])
            continue
        seen_goals.add(goal_key)

        stack = _infer_stack(workdir)
        recent_files = _list_recent_files(workdir)
        domain = _classify_domain(workdir, best_msg)

        # Build recent_activity from file list + the message itself
        activity: list[str] = [f"edit {f}" for f in recent_files[:5]]
        activity.append(f"intent: {best_msg[:80]}")

        ctx = BehavioralContext(
            inferred_stack=stack,
            active_domains=[domain],
            recent_activity=activity,
            project_count=1,
            artifact_count=0,
        )

        ex = DatasetExample(
            source="extracted",
            behavioral_context=ctx,
            true_goal=best_msg[:400],
            true_domain=domain,
            expected_confidence=0.6,  # conservative — extracted labels are silver
            quality_score=3,
            notes=f"extracted from tg-claude workdir={workdir}",
        )

        errs = ex.validate()
        if errs:
            log.warning("extracted example invalid (workdir=%s): %s", workdir, errs)
            continue

        examples.append(ex)
        log.info("extracted: domain=%s goal=%s", domain, best_msg[:60])

    log.info("extraction complete: %d examples from %d sessions", len(examples), len(rows))
    return examples
