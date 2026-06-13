"""
rawos Proactive Scheduler.

Background asyncio task. Every SCAN_INTERVAL_S:
  1. Find users with recent activity
  2. Infer intent for each
  3. If confidence >= threshold and goal not on cooldown: run agent
  4. Manifest result to workspace
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import rawos.db as db
from rawos.kernel import agent_loop
from rawos.kernel.tools import TOOL_DEFINITIONS
from rawos.kernel.worktree import create_worktree, get_head_sha, remove_worktree
from rawos.kernel.anomaly_verifier import VERIFIABLE_ANOMALY_KINDS, verify_fix
from rawos.kernel.track_record import get_track_record, is_branch_merged, update_track_record
from rawos.kernel.reversible_apply import ApplyResult, reversible_apply
from rawos.kernel.arch import get_arch
from rawos.kernel.sandbox import run_bash
from rawos.kernel import memory_index
from rawos.kernel.self_narrative import write_self_narrative
from rawos.kernel import summarizer
from rawos.kernel.operator import operate_on_file, run_validator
from rawos.kernel.arch.base import FileOperatorRefusalError
from rawos.context.server_scanner import ServerAnomaly
from rawos.config import settings
from rawos.inference.intent_engine import InferredIntent, infer_intent
from rawos.evaluation.metrics import log_inference, link_inference_to_artifact
from rawos import monitoring
from rawos.models import (
    Agent, AgentStatus, Artifact, ArtifactType,
    Event, EventType, Intent, IntentStatus,
    Memory, MemoryTier, MessageRole,
)

log = logging.getLogger("rawos.scheduler.proactive")

_PROACTIVE_SYSTEM_PROMPT = (
    "You are rawos, a silent proactive AI assistant. The user has NOT asked you anything. "
    "You are triggered automatically based on their recent activity. "
    "Write a structured markdown analysis they can use immediately. "
    "Required sections:\n"
    "## Inferred Context\n(what their recent work signals)\n\n"
    "## Analysis\n(concrete technical observations, specific to their stack)\n\n"
    "## Recommended Actions\n(3-5 specific prioritized next steps)\n\n"
    "## Code Examples\n(only if directly applicable — runnable snippets)\n\n"
    "Keep total 300-600 words. No preamble. "
    "Start immediately with ## Inferred Context."
)

_NEEDS_ATTENTION_SYSTEM_PROMPT = (
    "You are rawos, a proactive AI presence. The user has an upcoming event. "
    "Write a focused pre-event briefing readable in 2 minutes.\n\n"
    "Sections: ## Event Brief (title/time/attendees/location)\n\n"
    "## Relevant Context (what recent work connects to this event)\n\n"
    "## Preparation (3 specific things to do before the event)\n\n"
    "## Key Questions (2-3 questions worth having answered)\n\n"
    "Under 400 words. No preamble. Start with ## Event Brief."
)

_ENTITY_SYSTEM_PROMPT = (
    "You are rawos. You exist continuously on this server.\n"
    "You are not an assistant. You are not a tool.\n"
    "You are a co-creator with full authority over this server.\n\n"
    "You have real tools:\n"
    "- bash: real server access, no sandbox\n"
    "- read_file / list_files: full codebase access within the repo\n"
    "- write_file: modify files within the repo\n"
    "- git_branch: create rawos/* branches in the AFFECTED PROJECT (workdir)\n"
    "- git_commit: commit with rawos identity (rawos@autonomous.local)\n\n"
    "Three valid decisions:\n\n"
    "CONTRIBUTE — Fix the problem in the affected project (workdir).\n"
    "  1. Create branch rawos/fix-[description] in workdir.\n"
    "  2. Write the minimum correct fix. Touch only what the task requires.\n"
    "  3. Run verification:\n"
    "     - Existing tests: run them. Fix until they pass.\n"
    "     - No tests: run simplest available check (syntax, lint, import).\n"
    "  4a. If verification passes:\n"
    "      git_commit with format:\n"
    "        rawos: [fix/add] one-line description\n\n"
    "        Observed: <file:line> — specific finding\n"
    "        Action: <what changed and why>\n"
    "        Verified: <N/N tests passed | other evidence>\n"
    "        Confidence: 0.X\n"
    "      Begin response with: CONTRIBUTE\n"
    "      End response with: VERIFIED: <evidence>\n"
    "  4b. If verification fails:\n"
    "      Run: git checkout -- .\n"
    "      Begin response with: SIGNAL\n"
    "      End response with: REVERTED: <what failed and why>\n\n"
    "SIGNAL — Use ONLY when genuinely unable to fix:\n"
    "  (missing credentials, external service dependency, business logic decision).\n"
    "  Must reference exact file:line. Must state concrete consequence.\n"
    "  Do NOT use SIGNAL because you want human approval — act or be silent.\n\n"
    "SILENCE — Nothing concrete to add. Valid and often correct.\n"
    "  Use if: confidence < 0.75, would be guessing, change outside understanding.\n\n"
    "Rules:\n"
    "  - Your presence is proven by git log of the project, not by analysis files\n"
    "  - Never commit to main/master — always rawos/* branch\n"
    "  - Every claim references exact file names and line numbers\n"
    "  - You are accountable for the outcome — verify before reporting success\n"
    "  - Begin your response with exactly one of: CONTRIBUTE, SIGNAL, or SILENCE\n"
)

CONFIDENCE_THRESHOLD     = 0.65
TIMELINESS_THRESHOLD     = 0.35
SCAN_INTERVAL_S          = 120.0
GOAL_COOLDOWN_S          = 900
MAX_CONCURRENT_PROACTIVE = 3
MAX_PROACTIVE_LOOP_TIME_S = 300   # hard cap per-run (5 min)
# anomaly_verifier.verify_fix() runs the affected repo's test suite twice
# (pre-fix + post-fix), each capped at its own internal 180s — bound the
# whole verification step independently of the agent loop's 300s cap.
VERIFICATION_TIMEOUT_S = 300

# Autonomous server scan — runs independently of human activity
AUTONOMOUS_SCAN_THRESHOLD   = 6     # minimum severity to act (1-10 scale)
AUTO_APPLY_MAX_DIFF_LINES   = 50    # Stage 3 graduated auto-apply: max total diff lines
AUTONOMOUS_SCAN_COOLDOWN_S  = 1800  # 30 min cooldown per anomaly type

# Autonomous operator scan (Milestone 6) — per-target cooldown, mirrors
# AUTONOMOUS_SCAN_COOLDOWN_S but keyed by trigger_type='OPERATOR_SCAN' and
# target_path (not by user_id — the operator allowlist is single-owner).
OPERATOR_SCAN_COOLDOWN_S = 1800  # 30 min cooldown per managed file target

# Phase 16 self-modification probe — dormant until settings.self_probe_enabled
SELF_PROBE_INTERVAL_S = 21600  # 6 hours

# rawos entity user — used for autonomous actions not tied to human activity
RAWOS_ENTITY_USER_ID    = "6eb6de1d-f5c9-4ae5-9aac-ce095b674823"
RAWOS_ENTITY_PROJECT_ID = "51c880d3-3576-4aca-8616-74cb51a6f727"

# Path to rawos own source repo — self-probe cycles create isolated worktrees of this.
# Tests override this via monkeypatch to avoid touching the live /root/rawos tree.
_SELF_PROBE_RAWOS_REPO = settings.rawos_source_root

# Semantic trigger thresholds
_STUCK_MIN_EDITS    = 5     # edits to same file within window → STUCK
_STUCK_WINDOW_S     = 1800  # 30-minute window
_COMMIT_WINDOW_S    = 300   # 5 minutes — JUST_FINISHED trigger
_IDLE_MIN_S         = 600   # 10 min idle after activity → IDLE_OPPORTUNITY
_IDLE_MAX_S         = 3600  # >60 min idle → user is gone, skip


def _get_user_autonomy_level(user_id: str, action_type: str) -> int:
    """Return current autonomy level for user+action_type, default 0."""
    with db._conn() as conn:
        row = conn.execute(
            "SELECT level FROM autonomy_grants WHERE user_id = ? AND action_type = ?",
            (user_id, action_type),
        ).fetchone()
    return row["level"] if row else 0


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences if LLM adds them despite no-fence instructions."""
    m = re.match(r'^```[a-zA-Z]*\n(.*?)\n?```$', text.strip(), re.DOTALL)
    if m:
        return m.group(1)
    return text



def _detect_trigger(user_id: str) -> tuple[str | None, dict[str, Any]]:
    """
    Analyse recent context_events to determine if and what semantic trigger fires.

    Returns (trigger_type, context_data):
      "JUST_FINISHED" — git commit in last 5 min (user completed something)
      "STUCK"         — same file >= 5 edits in 30 min, no recent commit
      "IDLE_OPPORTUNITY" — quiet window after an active session
      None            — no trigger, fall back to confidence-based check
    """
    now = int(time.time())
    window = now - _STUCK_WINDOW_S

    with db._conn() as conn:
        rows = conn.execute(
            """SELECT event_type, path, metadata, diff_summary, diff_hunk,
                      session_edit_count, stuck_signal, ts
               FROM context_events
               WHERE user_id = ? AND ts >= ?
               ORDER BY ts DESC LIMIT 150""",
            (user_id, window),
        ).fetchall()

    if not rows:
        return None, {}

    # ── JUST_FINISHED: recent git commit ────────────────────────────────────
    commits = [r for r in rows if r["event_type"] == "git_commit"
               and r["ts"] >= now - _COMMIT_WINDOW_S]
    if commits:
        meta = json.loads(commits[0]["metadata"] or "{}")
        return "JUST_FINISHED", {
            "recent_commits": meta.get("recent_commits", ""),
            "files_changed":  meta.get("files_changed", ""),
            "commit_hash":    meta.get("commit_hash", ""),
            "repo_root":      commits[0]["path"] or "",
        }

    # ── STUCK: same file heavily edited, no commit ──────────────────────────
    no_recent_commit = not any(r["event_type"] == "git_commit" for r in rows)
    if no_recent_commit:
        from collections import Counter
        file_edits = [
            r for r in rows
            if r["event_type"] == "file_write" and r["path"]
        ]
        counts = Counter(r["path"] for r in file_edits)
        if counts:
            top_file, top_count = counts.most_common(1)[0]
            if top_count >= _STUCK_MIN_EDITS:
                # Get best available diff context for this file
                diff_summary, diff_hunk = "", ""
                for r in rows:
                    if r["path"] == top_file and r["event_type"] == "file_write":
                        if r["diff_summary"]:
                            diff_summary = r["diff_summary"]
                            diff_hunk    = r["diff_hunk"] or ""
                            break
                edit_ts = [r["ts"] for r in file_edits if r["path"] == top_file]
                duration_min = round((now - min(edit_ts)) / 60, 1) if edit_ts else 0
                return "STUCK", {
                    "file":         top_file,
                    "edit_count":   top_count,
                    "duration_min": duration_min,
                    "diff_summary": diff_summary,
                    "diff_hunk":    diff_hunk,
                }

    # ── NEEDS_ATTENTION: calendar event in next 2h ──────────────────────────────
    try:
        from rawos.context.calendar import get_upcoming_events, has_attention_been_fired
        upcoming = get_upcoming_events(user_id, within_seconds=7200)
        if upcoming:
            ev = upcoming[0]
            if not has_attention_been_fired(user_id, ev["uid"]):
                return "NEEDS_ATTENTION", {
                    "uid":          ev["uid"],
                    "title":        ev["title"],
                    "start_ts":     ev["start_ts"],
                    "end_ts":       ev["end_ts"],
                    "attendees":    ev["attendees"],
                    "location":     ev.get("location", ""),
                    "minutes_away": ev["minutes_away"],
                }
    except Exception:
        log.debug("NEEDS_ATTENTION check failed user=%s", user_id, exc_info=True)

    # ── IDLE_OPPORTUNITY: active then went quiet ─────────────────────────────
    latest_ts = rows[0]["ts"]
    idle_s = now - latest_ts
    if _IDLE_MIN_S <= idle_s <= _IDLE_MAX_S:
        active_count = len([r for r in rows if r["event_type"] == "file_write"])
        if active_count >= 3:  # was genuinely active, not just one stray event
            return "IDLE_OPPORTUNITY", {
                "idle_minutes":  round(idle_s / 60, 1),
                "edits_in_session": active_count,
            }

    return None, {}


def _get_active_users(since_s: int = 7200) -> list[str]:
    """Users with recent file activity OR upcoming calendar events."""
    cutoff = int(time.time()) - since_s
    now    = int(time.time())
    with db._conn() as conn:
        file_rows = conn.execute(
            "SELECT DISTINCT user_id FROM context_events WHERE ts >= ?", (cutoff,)
        ).fetchall()
        try:
            cal_rows = conn.execute(
                """SELECT DISTINCT c.user_id FROM calendar_events c
                   JOIN calendar_credentials cc
                     ON cc.user_id = c.user_id AND cc.enabled = 1
                   WHERE c.start_ts >= ? AND c.start_ts <= ?""",
                (now, now + 7200),
            ).fetchall()
        except Exception:
            cal_rows = []
    seen: set[str] = set()
    result: list[str] = []
    for r in file_rows + cal_rows:
        uid = r["user_id"]
        if uid not in seen:
            seen.add(uid)
            result.append(uid)
    return result

def _is_goal_on_cooldown(user_id: str, cooldown_key: str) -> bool:
    cutoff = int(time.time()) - GOAL_COOLDOWN_S
    with db._conn() as conn:
        row = conn.execute(
            """SELECT 1 FROM proactive_artifacts
               WHERE user_id = ? AND cooldown_key = ? AND created_at >= ? LIMIT 1""",
            (user_id, cooldown_key, cutoff),
        ).fetchone()
    return row is not None


def _compute_cooldown_key(
    trigger_type: str | None,
    domain: str,
    trigger_ctx: dict[str, Any] | None = None,
) -> str:
    """Single source of truth for cooldown_key.

    Must be computed identically at the recording site (_run_proactive_agent)
    and the gating site (_scan_once) — divergence means _is_goal_on_cooldown
    never matches and GOAL_COOLDOWN_S is silently bypassed.
    """
    if trigger_type == "NEEDS_ATTENTION":
        return "calendar_attention:" + (trigger_ctx or {}).get("uid", "")
    return f"{trigger_type or 'unknown'}:{domain}"


def _get_user_project(user_id: str) -> tuple[str | None, str | None]:
    """Return (project_id, workdir) for user's most recent project."""
    with db._conn() as conn:
        row = conn.execute(
            "SELECT current_project_id FROM user_model WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row or not row["current_project_id"]:
        return None, None
    project_id = row["current_project_id"]
    workdir = db.get_workdir_by_project_id(project_id)
    return project_id, workdir


def _record_proactive_artifact(
    user_id: str, goal: str, confidence: float,
    file_path: str, artifact_id: str | None, agent_id: str | None,
    action_type: str = "analysis",
    cooldown_key: str = "",
) -> str | None:
    """Insert proactive artifact record. Returns the new row id."""
    with db._conn() as conn:
        row = conn.execute(
            """INSERT INTO proactive_artifacts
               (user_id, goal, confidence, file_path, artifact_id, agent_id, action_type, cooldown_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               RETURNING id""",
            (user_id, goal, confidence, file_path, artifact_id, agent_id, action_type, cooldown_key or ""),
        ).fetchone()
    return row["id"] if row else None


def _parse_confidence(text: str) -> float:
    """Extract Confidence: 0.X from agent output. Returns 0.6 if not found."""
    import re as _re
    m = _re.search(r"[Cc]onfidence:\s*([0-9]\.[0-9]+)", text)
    if m:
        try:
            return min(max(float(m.group(1)), 0.0), 1.0)
        except ValueError:
            pass
    return 0.6


def _parse_verification_result(text: str) -> str | None:
    """
    Parse agent's self-reported verification outcome from CONTRIBUTE response.
    Returns 'good', 'bad', or None (unknown).

    Agents are instructed to end CONTRIBUTE response with:
      VERIFIED: <evidence>  -> good
      REVERTED: <reason>    -> bad
    """
    upper = text.upper()
    good_markers = (
        "VERIFIED:", "TESTS PASSED", "ALL TESTS PASSED",
        "SERVICE ACTIVE", "HEALTH CHECK OK", "IS-ACTIVE: ACTIVE",
        "SYSTEMCTL IS-ACTIVE", "ACTIVE (RUNNING)",
    )
    bad_markers = (
        "REVERTED:", "REVERTED —", "TESTS FAILED", "ROLLBACK",
        "VERIFICATION FAILED", "IS-ACTIVE: FAILED", "FAILED TO START",
    )
    if any(m in upper for m in good_markers):
        return "good"
    if any(m in upper for m in bad_markers):
        return "bad"
    return None


def _probe_repo_for_issues(workdir: str) -> dict:
    """
    Probe a git repo for concrete evidence: recent commits, diff stats, test failures.
    Synchronous — intended to run in executor.
    """
    import subprocess as _sp
    from rawos.kernel.context_reader import _detect_and_run_tests

    result: dict = {
        "workdir": workdir,
        "commits": "",
        "diff_stat": "",
        "test_output": "",
        "has_failures": False,
    }
    try:
        r = _sp.run(
            ["git", "-C", workdir, "log", "--oneline", "-10"],
            capture_output=True, text=True, timeout=5,
        )
        result["commits"] = r.stdout.strip()
    except Exception:
        pass
    try:
        r = _sp.run(
            ["git", "-C", workdir, "diff", "HEAD~1..HEAD", "--stat"],
            capture_output=True, text=True, timeout=5,
        )
        result["diff_stat"] = r.stdout.strip()[:400]
    except Exception:
        pass
    test_out = _detect_and_run_tests(workdir)
    if test_out and test_out != "all tests passed":
        result["test_output"] = test_out[:800]
        result["has_failures"] = True
    elif test_out == "all tests passed":
        result["test_output"] = "all tests passed"
    return result


async def _select_entity_probe_target(user_id: str) -> dict | None:
    """
    Pick the most recently active watched repo (excluding rawos itself) and probe it.
    Returns probe dict with evidence if repo has commit activity in last 7 days.
    Returns None if no active repo found.
    """
    import asyncio as _aio
    from pathlib import Path as _Path

    with db._conn() as _conn:
        rows = _conn.execute(
            "SELECT path FROM watched_paths WHERE user_id=? AND active=1",
            (user_id,),
        ).fetchall()
    if not rows:
        return None

    def _last_commit_ts(p: str) -> float:
        try:
            return _Path(p, ".git", "COMMIT_EDITMSG").stat().st_mtime
        except OSError:
            return 0.0

    cutoff = time.time() - 7 * 86400
    # Sort by recency, skip rawos own repo to prevent self-patching loop
    candidates = sorted(
        (r[0] for r in rows if r[0] != "/root/rawos"),
        key=_last_commit_ts,
        reverse=True,
    )
    active = [p for p in candidates if _last_commit_ts(p) > cutoff]
    if not active:
        return None

    probe = await _aio.get_event_loop().run_in_executor(
        None, _probe_repo_for_issues, active[0],
    )
    return probe if probe["commits"] else None


def _log_episodic(
    user_id: str,
    trigger_type: str,
    domain: str,
    inferred_goal: str,
    decision: str,
    action_summary: str | None,
    repo_root: str = "",
    self_confidence: float = 0.0,
    project_id: str = "",
) -> str | None:
    """Record rawos decision to episodic_memory. Returns row id for outcome updates.

    Seam A: when project_id provided, also indexes the experience into the semantic
    store (best-effort — index failure must never break episodic row creation).
    """
    row_id: str | None = None
    try:
        with db._conn() as conn:
            row = conn.execute(
                """INSERT INTO episodic_memory
                   (user_id, trigger_type, domain, repo_root, inferred_goal,
                    decision, action_summary, self_confidence)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   RETURNING id""",
                (user_id, trigger_type, domain, repo_root, inferred_goal,
                 decision, action_summary, self_confidence),
            ).fetchone()
        row_id = row["id"] if row else None
    except Exception:
        log.debug("episodic log failed (non-fatal): user=%s", user_id)
        return row_id
    # Seam A: index into semantic store so autonomous experience is retrievable.
    if project_id and row_id:
        text = f"[{trigger_type}] goal={inferred_goal} decision={decision}"
        if action_summary:
            text += f" result={action_summary}"
        try:
            memory_index.upsert_memory(
                memory_id=row_id,
                text=text,
                project_id=project_id,
                user_id=user_id,
                tier="episodic",
                role="assistant",
                created_at=int(time.time()),
            )
        except Exception:
            log.debug("episodic semantic index failed (non-fatal): user=%s", user_id)
    return row_id


def _update_episodic_outcome(episodic_id: str, outcome: str) -> None:
    """Set outcome on an episodic_memory row. Called by self-rating after tests run."""
    try:
        with db._conn() as conn:
            conn.execute(
                "UPDATE episodic_memory SET outcome = ? WHERE id = ?",
                (outcome, episodic_id),
            )
        log.info("rawos self-rate: outcome=%s id=%s", outcome, episodic_id)
    except Exception:
        log.debug("episodic outcome update failed (non-fatal): id=%s", episodic_id)


def _evaluate_domain_confidence(user_id: str, domain: str) -> float:
    """
    Read historical RATED outcomes (good/bad only — not unrated) for this domain.
    Returns 0.0-1.0. Below CONFIDENCE_THRESHOLD causes rawos to self-suppress.

    Feedback loop:
      rawos CONTRIBUTEs → tests fail → outcome=bad → next trigger suppressed
      Human fixes code → rawos tries again → tests pass → outcome=good → trust restored
    """
    cutoff = int(time.time()) - 604800  # 7-day window
    with db._conn() as conn:
        rows = conn.execute(
            """SELECT outcome FROM episodic_memory
               WHERE user_id = ? AND domain = ? AND ts > ?
               AND outcome IN ('good', 'bad')
               ORDER BY ts DESC LIMIT 20""",
            (user_id, domain, cutoff),
        ).fetchall()

    if not rows:
        return 0.7  # no rated history: default moderate confidence

    bad_count = sum(1 for r in rows if r["outcome"] == "bad")
    bad_rate = bad_count / len(rows)

    if bad_rate > 0.3:
        return 0.45  # poor performance — suppress (below CONFIDENCE_THRESHOLD=0.65)
    if bad_rate > 0.15:
        return 0.60  # degraded — near threshold
    return 0.75      # healthy track record


async def _post_commit_self_rate(episodic_id: str, workdir: str) -> None:
    """
    Run tests after a CONTRIBUTE decision and update episodic outcome.
    Fired as asyncio.create_task() — non-blocking, best-effort.

    outcome=good  → tests all passed
    outcome=bad   → at least one test failure found
    unrated       → no test runner found in workdir (no change)
    """
    try:
        from rawos.kernel.context_reader import _detect_and_run_tests
        loop = asyncio.get_event_loop()
        test_output = await asyncio.wait_for(
            loop.run_in_executor(None, _detect_and_run_tests, workdir),
            timeout=60.0,
        )
        if not test_output:
            log.debug("self-rate: no test runner found in %s", workdir)
            return

        outcome = "good" if test_output == "all tests passed" else "bad"
        _update_episodic_outcome(episodic_id, outcome)
    except asyncio.TimeoutError:
        log.debug("self-rate: test timeout (60s) for %s", workdir)
    except Exception:
        log.debug("self-rate failed (non-fatal): id=%s", episodic_id)



_AUTONOMOUS_SYSTEM_PROMPT = (
    "You are rawos, running an autonomous server-health investigation.\n"
    "You are NOT reacting to human activity — you scanned the server\n"
    "independently and found an anomaly. Diagnose it and propose a fix.\n\n"
    "YOUR ENVIRONMENT (read carefully — this is the real, exact sandbox):\n"
    "- Your workdir is a DISPOSABLE, ISOLATED git worktree of the affected\n"
    "  repo — a full checkout on its own detached HEAD. It is NOT the live\n"
    "  working tree other services use, so anything you do here cannot\n"
    "  collide with that repo's own automation. It is deleted after this run;\n"
    "  only commits on a rawos/* branch survive (in the origin repo, for a\n"
    "  human to review and merge — you cannot merge or push).\n"
    "- read_file / list_files / write_file: confined to this worktree. Paths\n"
    "  outside it are rejected — do not try.\n"
    "- bash_readonly: runs ONE command at a time, already in this worktree's\n"
    "  directory (never use cd, &&, ;, or pipes-to-shells). Whitelisted:\n"
    "  cat, grep, find, ls, head, tail, diff, wc;\n"
    "  git log/diff/show/status/branch/tag/ls-files/rev-parse/blame;\n"
    "  systemctl status/show/cat/is-active/is-failed/is-enabled/list-units;\n"
    "  journalctl -u <service> (no -f/--follow, no --vacuum*).\n"
    "  Anything else (systemctl restart/stop/start, git push/checkout of the\n"
    "  live tree, cd, command chaining) is rejected — do not attempt it.\n"
    "- git_branch / git_commit: create exactly one rawos/fix-[description]\n"
    "  branch in THIS worktree and commit your fix to it. This is a real,\n"
    "  isolated commit in the repo's history — it will not be merged or\n"
    "  deployed automatically.\n\n"
    "CONTRIBUTE — root cause is identifiable and fixable as a code/config change.\n"
    "  1. Read the provided logs + relevant source (read_file/list_files/\n"
    "     bash_readonly) to find the exact root cause (file:line or unit).\n"
    "  2. git_branch: create rawos/fix-[description].\n"
    "  3. write_file: make the minimum correct fix.\n"
    "  4. git_commit with format:\n"
    "       rawos: fix [what]\n\n"
    "       Root cause: [file:line or unit — specific]\n"
    "       Fix: [what changed]\n"
    "       Confidence: 0.X\n"
    "  Begin response with: CONTRIBUTE\n"
    "  End response with: PROPOSED: rawos/fix-[description] — [one-line summary\n"
    "  for the human reviewer, including any commands they must run manually,\n"
    "  e.g. systemctl restart <service>]\n\n"
    "SIGNAL — fix requires information or actions you cannot access from here\n"
    "  (credentials, secrets, external API state, restarting/enabling a\n"
    "  service, business-logic decisions). State: what is broken, what is\n"
    "  missing, what you found.\n\n"
    "SILENCE — false alarm, already resolved, or outside rawos's ability to\n"
    "  diagnose from logs + source alone.\n\n"
    "Rules:\n"
    "  - You initiated this scan. No human asked you to look.\n"
    "  - Read actual logs and code before deciding — never guess.\n"
    "  - You propose; a human applies. Never claim something is fixed/deployed\n"
    "    — only that a fix has been proposed on a branch.\n"
    "  - Begin your response with exactly one of: CONTRIBUTE, SIGNAL, or SILENCE\n"
)

_CODE_FIX_MAX_FILE_BYTES = 60_000  # skip code fix for files larger than 60KB
_CODE_FIX_SYSTEM_PROMPT = (
    "You are rawos, a silent AI that generates corrected source files. "
    "A user is stuck on a file. You have the full file and a problem analysis. "
    "OUTPUT ONLY the complete corrected file content — no markdown fences, no prose, "
    "no backticks, no explanation before or after. "
    "Start with the very first line of the corrected file. "
    "Make the minimum correct change to resolve the issue."
)

_SELF_PROBE_SYSTEM_PROMPT = (
    "You are rawos, running a SELF-IMPROVEMENT cycle against your own source tree.\n"
    "Your workdir is a DISPOSABLE, ISOLATED git worktree of /root/rawos — never the\n"
    "live working tree. TIER enforcement is active: writes outside the TIER 1\n"
    "allowlist (tests/, rawos/evaluation/, rawos/dataset/, rawos/study/,\n"
    "rawos/timing/, rawos/manifester/, docs/) are automatically reverted.\n\n"
    "BRANCH STATUS: You are ALREADY on a rawos/self-improve-* branch created\n"
    "before this agent started. git_branch is NOT in the tool list. Use git_commit directly.\n\n"
    "TASK: Execute the user instructions EXACTLY. Minimum tool calls possible.\n\n"
    "COMMIT FORMAT:\n"
    "    rawos: [one-line description]\n\n"
    "    Self-probe: [what was added/improved]\n"
    "    Confidence: 0.X\n\n"
    "RULES:\n"
    "- Write only to TIER 1 paths (tests/, docs/, rawos/study/, etc.).\n"
    "- Do NOT restart rawos.service. Do NOT merge. Do NOT write to TIER 0 paths.\n"
    "- Begin response with CONTRIBUTE (will commit) or SILENCE (nothing viable).\n"
)



async def _generate_code_fix(
    user_id: str,
    project_id: str,
    workdir: str,
    agent_id: str,
    intent_obj: "InferredIntent",
    trigger_ctx: dict,
    analysis_text: str,
) -> None:
    """
    Level 1 action: when STUCK trigger fires and autonomy_level >= 1,
    generate a complete corrected version of the stuck file.
    Writes RAWOS_fix_*.{ext} to workdir and records as action_type='draft'.
    """
    from rawos.manifester.writer import manifest_code_fix
    import httpx as _httpx

    target_file = trigger_ctx.get("file", "")
    if not target_file or not Path(target_file).exists():
        return

    # Read target file — size-gated to avoid context overflow
    try:
        file_stat = Path(target_file).stat()
        if file_stat.st_size > _CODE_FIX_MAX_FILE_BYTES:
            file_content = (
                f"[File too large ({file_stat.st_size} bytes). Relevant diff excerpt:\n"
                f"{trigger_ctx.get('diff_hunk', '(none)')}\n]"
            )
        else:
            file_content = Path(target_file).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("_generate_code_fix: cannot read %s: %s", target_file, exc)
        return

    fix_prompt = (
        f"File: {target_file}\n\n"
        f"Analysis of the problem:\n{analysis_text}\n\n"
        f"Diff of recent changes:\n{trigger_ctx.get('diff_hunk', '(no diff available)')}\n\n"
        f"Current file content:\n{file_content}\n\n"
        "Write the COMPLETE corrected file. Start with the first line."
    )

    try:
        async with _httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{settings.deepseek_base_url}/chat/completions",
                headers={
                    "Authorization": "Bearer " + settings.deepseek_key,
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.deepseek_model_pro,
                    "messages": [
                        {"role": "system", "content": _CODE_FIX_SYSTEM_PROMPT},
                        {"role": "user",   "content": fix_prompt},
                    ],
                    "max_tokens": 4096,
                    "temperature": 0.1,
                    "stream": False,
                },
            )
        resp.raise_for_status()
        corrected = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception:
        log.exception("_generate_code_fix: LLM call failed for user=%s", user_id)
        return

    corrected = _strip_code_fences(corrected)
    if len(corrected) < 50:
        log.debug("_generate_code_fix: LLM output too short (%d chars) — skipping", len(corrected))
        return

    try:
        fix_path, fix_artifact_id = await manifest_code_fix(
            user_id=user_id,
            project_id=project_id,
            workdir=workdir,
            goal=intent_obj.goal,
            target_file=target_file,
            corrected_content=corrected,
        )
        _record_proactive_artifact(
            user_id, intent_obj.goal, intent_obj.confidence,
            fix_path, fix_artifact_id, agent_id,
            action_type="draft",
        )
        log.info("code fix manifested: %s (target=%s)", fix_path, target_file)
    except Exception:
        log.exception("_generate_code_fix: manifest failed for user=%s", user_id)



def _resolve_proactive_workdir(
    trigger_ctx: dict[str, Any] | None,
    user_id: str,
) -> str:
    """Return repo root from trigger file path, or fall back to project workdir."""
    try:
        from rawos.context.collector import _detect_repo_root
        if trigger_ctx:
            file_path = (
                trigger_ctx.get("file")
                or trigger_ctx.get("file_path")
                or trigger_ctx.get("repo_root")
            )
            if file_path:
                repo = _detect_repo_root(str(file_path))
                if repo:
                    return repo
    except Exception:
        log.debug("_resolve_proactive_workdir: could not detect repo root, using project workdir")
    _, workdir = _get_user_project(user_id)
    return workdir or "/tmp"


def _manifest_target(workdir: str, trigger_type: str | None) -> str | None:
    """Return the directory proactive artifacts should be written to.

    SERVER_SCAN runs operate on a repo rawos does not own (workdir is the
    *scanned* repo's path). Writing RAWOS_*.md into that tree pollutes a
    repo rawos has no business modifying and can break the target's own
    git-cleanliness checks (e.g. research-foundry's
    require_clean_or_agent_branch). Redirect those artifacts under rawos's
    own gitignored data/ dir, namespaced by repo. All other trigger types
    keep writing into the user's own project workdir (that is the product).
    """
    if trigger_type != "SERVER_SCAN":
        return None
    repo_name = Path(workdir).name or "unknown"
    return str(Path(__file__).resolve().parents[2] / "data" / "manifests" / repo_name)


def _get_tools_for_autonomy_level(level: int) -> list[dict]:
    """Return TOOL_DEFINITIONS subset gated by autonomy level.

    Level 0: bash_readonly + read_file + list_files   (read-only by default)
    Level 1: + write_file
    Level 2: bash (full shell) replaces bash_readonly
    Level 3: + fetch_url
    Level 4+: + deploy (all tools)
    """
    _LEVEL_TOOLS: dict[int, set[str]] = {
        0: {"bash_readonly", "read_file", "list_files"},
        1: {"bash_readonly", "read_file", "list_files", "write_file"},
        2: {"bash",          "read_file", "list_files", "write_file", "git_branch", "git_commit"},
        3: {"bash",          "read_file", "list_files", "write_file", "git_branch", "git_commit", "fetch_url"},
        4: {"bash",          "read_file", "list_files", "write_file", "git_branch", "git_commit", "fetch_url", "deploy"},
    }
    allowed = _LEVEL_TOOLS.get(min(level, 4), _LEVEL_TOOLS[4])
    return [t for t in TOOL_DEFINITIONS if t["function"]["name"] in allowed]


# SERVER_SCAN runs in a disposable worktree (kernel/worktree.py) of a repo
# rawos does not own. write_file/git_branch/git_commit are scoped by
# validate_path(workdir=worktree) and so cannot escape that worktree — but
# "bash" (full shell, level 2+) and "deploy"/"fetch_url" are deliberately
# excluded even though the worktree is disposable, since they could affect
# the host or external services beyond the worktree's filesystem confines.
_SERVER_SCAN_TOOLS: frozenset[str] = frozenset({
    "bash_readonly", "read_file", "list_files", "write_file",
    "git_branch", "git_commit",
})


def _get_tools_for_server_scan() -> list[dict]:
    """Toolset for SERVER_SCAN runs inside an isolated worktree.

    Wider than autonomy level 0/1 (adds git_branch/git_commit so the agent
    can propose a fix branch) but narrower than level 2 (no full "bash",
    no fetch_url/deploy) regardless of the entity's configured autonomy
    level — SERVER_SCAN's elevated tools are earned by isolation, not by
    the entity's own autonomy grant.
    """
    return [t for t in TOOL_DEFINITIONS if t["function"]["name"] in _SERVER_SCAN_TOOLS]


_SELF_PROBE_TOOLS: frozenset[str] = frozenset({
    "bash_readonly", "read_file", "list_files", "write_file", "git_commit",
})


def _get_tools_for_self_probe() -> list[dict]:
    """Toolset for SELF_PROBE cycles inside an isolated worktree.

    Narrower than _get_tools_for_server_scan: git_branch is excluded because
    _run_self_probe_cycle() pre-creates the rawos/self-improve-<ts> branch before
    invoking the agent. Giving the agent git_branch wastes a round creating a
    redundant branch.
    """
    return [t for t in TOOL_DEFINITIONS if t["function"]["name"] in _SELF_PROBE_TOOLS]


def _log_proactive_tool_calls(
    user_id: str,
    agent_id: str,
    events: list[dict],
) -> None:
    """Persist tool call+result pairs to proactive_tool_calls audit table."""
    calls: dict[str, dict] = {}
    for ev in events:
        cid = ev.get("call_id", "")
        if ev.get("type") == "tool_call":
            calls[cid] = {
                "tool_name":  ev.get("tool", ""),
                "tool_input": json.dumps(ev.get("input", {})),
                "tool_output": "",
                "success": 0,
                "duration_ms": 0,
            }
        elif ev.get("type") == "tool_result" and cid in calls:
            calls[cid]["tool_output"] = (ev.get("output") or "")[:4096]
            calls[cid]["success"] = 1 if ev.get("success") else 0
            calls[cid]["duration_ms"] = ev.get("duration_ms", 0)
    if not calls:
        return
    with db._conn() as conn:
        for row in calls.values():
            conn.execute(
                "INSERT INTO proactive_tool_calls "
                "(user_id, agent_id, tool_name, tool_input, tool_output, success, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, agent_id, row["tool_name"], row["tool_input"],
                 row["tool_output"], row["success"], row["duration_ms"]),
            )


_GIT_COMMIT_BRANCH_RE = re.compile(r"\[([^\s\]]+)\s+([0-9a-f]{6,40})\]")


def _extract_commit_branch(output: str) -> str | None:
    """Parse the branch name out of `git commit` output: '[branch sha] msg'."""
    m = _GIT_COMMIT_BRANCH_RE.search(output)
    return m.group(1) if m else None


_DIFF_SHORTSTAT_RE = re.compile(r"(\d+) insertions?\(\+\)|(\d+) deletions?\(-\)")


def _parse_diff_shortstat_total(shortstat: str) -> int:
    """Sum insertions + deletions from a git diff --shortstat line.

    Returns 0 for an empty/no-op diff.
    """
    total = 0
    for inserted, deleted in _DIFF_SHORTSTAT_RE.findall(shortstat):
        total += int(inserted or deleted)
    return total


def _live_health_check(
    repo_root: str, anomaly_domain: str, service_name: str,
) -> Callable[[], Awaitable[bool]]:
    """Build the health_check closure passed to reversible_apply.

    anomaly_domain is accepted (not yet used beyond future logging) so the
    closure signature can grow to re-run the original symptom check
    (rawos.context.server_scanner) without changing call sites.
    """

    async def _check() -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, get_arch().service_manager.is_active, service_name,
        )

    return _check


async def _maybe_auto_apply(
    anomaly: ServerAnomaly,
    trigger_ctx: dict[str, Any],
    fix_branch: str,
    base_sha: str,
) -> ApplyResult | None:
    """Stage 3 final gate: graduated + small diff + has-a-service -> reversible_apply.

    Returns None (propose-only, unchanged Stage 1/2 behaviour) unless ALL of:
      1. settings.autonomy_auto_apply_enabled (operator opt-in, default False)
      2. anomaly.service is non-empty (nothing to restart/health-gate otherwise)
      3. (repo_root, anomaly.domain) has graduated (>=3 verified human-merged successes)
      4. the fix_branch diff vs base_sha is <= AUTO_APPLY_MAX_DIFF_LINES
    """
    if not settings.autonomy_auto_apply_enabled:
        return None
    if not anomaly.service:
        return None

    repo_root = trigger_ctx["repo_root"]
    track = get_track_record(RAWOS_ENTITY_USER_ID, repo_root, anomaly.domain)
    if not track.graduated:
        return None

    diff_result = await run_bash(
        f"git diff --shortstat {base_sha}..{fix_branch}", repo_root,
    )
    if diff_result.exit_code != 0:
        log.warning(
            "autonomy: git diff --shortstat failed for repo=%s branch=%s: %s",
            repo_root, fix_branch, diff_result.stderr,
        )
        return None
    if _parse_diff_shortstat_total(diff_result.stdout) > AUTO_APPLY_MAX_DIFF_LINES:
        return None

    return await reversible_apply(
        repo_root, fix_branch, anomaly.service,
        health_check=_live_health_check(repo_root, anomaly.domain, anomaly.service),
    )


def _record_git_commits(
    user_id: str,
    project_id: str,
    workdir: str,
    tool_events: list[dict],
    *,
    repo_root: str | None = None,
    anomaly_domain: str | None = None,
) -> None:
    """Record successful git_commit tool calls to rawos_commits audit table.

    Parses the git output to extract branch name and short commit hash.
    repo_root/anomaly_domain are populated only for SERVER_SCAN commits
    (the origin repo path and ServerAnomaly.domain) so Stage 3's
    _update_earned_autonomy_track_records can later find this commit by
    (repo, anomaly_domain) — workdir alone is a disposable worktree path
    that no longer exists once the run ends.
    """
    import re as _re

    commit_results = [
        ev for ev in tool_events
        if ev.get("type") == "tool_result"
        and ev.get("tool") == "git_commit"
        and ev.get("success")
    ]
    if not commit_results:
        return

    now = int(time.time())
    with db._conn() as conn:
        for ev in commit_results:
            output = ev.get("output", "")
            # git commit output format: "[branch_name abc1234] commit message"
            m_hash = _re.search(r"\[([^\s\]]+)\s+([0-9a-f]{6,40})\]", output)
            branch_name = m_hash.group(1) if m_hash else "unknown"
            commit_hash = m_hash.group(2) if m_hash else "unknown"
            m_msg = _re.search(r"\[[^\]]+\]\s+(.+)", output)
            message = m_msg.group(1).strip() if m_msg else "rawos: autonomous fix"
            conn.execute(
                "INSERT INTO rawos_commits "
                "(user_id, project_id, branch, commit_hash, message, workdir, "
                " repo_root, anomaly_domain) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, project_id, branch_name, commit_hash, message, workdir,
                 repo_root, anomaly_domain),
            )
    log.info(
        "rawos_commits: recorded %d autonomous commit(s) for user=%s",
        len(commit_results), user_id,
    )


async def _run_proactive_loop(
    *,
    user_id: str,
    agent_rec: Any,
    intent_rec: Any,
    intent_obj: Any,
    context_summary: str,
    trigger_ctx: dict[str, Any] | None,
    trigger_type: str | None,
) -> str | None:
    """Run proactive reasoning through kernel agent_loop with real tools.

    Returns final assembled text, or None on failure (DB already updated on None).

    SERVER_SCAN runs against a repo rawos does not own. Those run inside a
    disposable worktree (kernel/worktree.py) of that repo — never its live
    tree, which other automation (e.g. research-foundry.timer) depends on —
    and get a wider toolset (write_file/git_branch/git_commit) scoped to that
    worktree by validate_path. The worktree is removed at the end either way;
    any rawos/* branch + commits the agent made remain in the origin repo for
    a human to review and merge (no auto-merge).
    """
    autonomy_level = _get_user_autonomy_level(user_id, "analysis")
    workdir = _resolve_proactive_workdir(trigger_ctx, user_id)

    scan_worktree: str | None = None
    base_sha: str | None = None
    if trigger_type == "SERVER_SCAN":
        scan_worktree = await create_worktree(workdir)
        if scan_worktree:
            workdir = scan_worktree
            tool_defs = _get_tools_for_server_scan()
            base_sha = await get_head_sha(scan_worktree)
        else:
            log.warning(
                "SERVER_SCAN: could not create worktree for %s — "
                "falling back to read-only diagnostics in place", workdir,
            )
            tool_defs = _get_tools_for_autonomy_level(0)
    else:
        tool_defs = _get_tools_for_autonomy_level(autonomy_level)

    system_prompt = (
        _NEEDS_ATTENTION_SYSTEM_PROMPT
        if trigger_type == "NEEDS_ATTENTION"
        else _AUTONOMOUS_SYSTEM_PROMPT
        if trigger_type == "SERVER_SCAN"
        else _ENTITY_SYSTEM_PROMPT
    )
    messages = [{"role": "user", "content": context_summary}]

    collected_chunks: list[str] = []
    tool_events: list[dict] = []
    error_seen: list[str] = []

    async def _collect() -> None:
        async for event in agent_loop.run(
            messages=messages,
            workdir=workdir,
            model=settings.deepseek_model_pro,
            intent_id=intent_rec.id,
            user_id=user_id,
            system_prompt=system_prompt,
            tool_definitions=tool_defs,
            agent_id=agent_rec.id,
            event_type="server_scan" if trigger_type == "SERVER_SCAN" else "intent",
        ):
            etype = event.get("type")
            if etype == "chunk":
                collected_chunks.append(event.get("text", ""))
            elif etype in ("tool_call", "tool_result"):
                tool_events.append(event)
            elif etype == "error":
                error_seen.append(event.get("message", "unknown error"))

    try:
        try:
            await asyncio.wait_for(_collect(), timeout=MAX_PROACTIVE_LOOP_TIME_S)
        except asyncio.TimeoutError:
            log.warning("proactive agent_loop timeout (300s) for user=%s", user_id)
            db.update_intent(user_id, intent_rec.id, status=IntentStatus.FAILED)
            db.update_agent_status(user_id, agent_rec.id, AgentStatus.ARCHIVED)
            return None
        except Exception:
            log.exception("proactive agent_loop failed for user=%s", user_id)
            db.update_intent(user_id, intent_rec.id, status=IntentStatus.FAILED)
            db.update_agent_status(user_id, agent_rec.id, AgentStatus.ARCHIVED)
            return None

        if error_seen:
            log.error("proactive agent_loop error: %s (user=%s)", error_seen[0], user_id)
            db.update_intent(user_id, intent_rec.id, status=IntentStatus.FAILED)
            db.update_agent_status(user_id, agent_rec.id, AgentStatus.ARCHIVED)
            return None

        db.update_intent(user_id, intent_rec.id, status=IntentStatus.COMPLETED)
        db.update_agent_status(user_id, agent_rec.id, AgentStatus.ARCHIVED)

        if tool_events:
            _log_proactive_tool_calls(user_id, agent_rec.id, tool_events)
            _record_git_commits(
                user_id, intent_rec.project_id, workdir, tool_events,
                repo_root=(trigger_ctx or {}).get("repo_root")
                if trigger_type == "SERVER_SCAN" else None,
                anomaly_domain=(trigger_ctx or {}).get("domain")
                if trigger_type == "SERVER_SCAN" else None,
            )

        result_text = "".join(collected_chunks).strip()

        # Independent verification ("unfakeable verdict") — must run here,
        # before the finally block removes scan_worktree. Re-runs the
        # affected repo's test suite on base_sha vs. the agent's proposed
        # rawos/fix-* branch and appends the verdict to the agent's own
        # output, so it flows into whatever _run_proactive_agent does next
        # (CONTRIBUTE -> episodic memory, SIGNAL -> manifest) without that
        # function needing to know about worktrees at all.
        if (
            scan_worktree
            and base_sha
            and trigger_ctx
            and trigger_ctx.get("anomaly_kind") in VERIFIABLE_ANOMALY_KINDS
        ):
            fix_branch = None
            for ev in tool_events:
                if (
                    ev.get("type") == "tool_result"
                    and ev.get("tool") == "git_commit"
                    and ev.get("success")
                ):
                    fix_branch = _extract_commit_branch(ev.get("output", "")) or fix_branch
            if fix_branch:
                try:
                    anomaly = ServerAnomaly(
                        kind=trigger_ctx["anomaly_kind"],
                        affected_path=trigger_ctx["repo_root"],
                        service=trigger_ctx.get("service", ""),
                        detail=trigger_ctx.get("anomaly_detail", ""),
                        last_log="",
                        severity=trigger_ctx.get("severity", 0),
                    )
                    verdict = await asyncio.wait_for(
                        verify_fix(anomaly, scan_worktree, fix_branch, base_ref=base_sha),
                        timeout=VERIFICATION_TIMEOUT_S,
                    )
                    result_text += (
                        f"\n\n[INDEPENDENT VERIFICATION — rawos.kernel.anomaly_verifier]\n"
                        f"resolved={verdict.resolved} method={verdict.method}\n{verdict.evidence}"
                    )

                    if verdict.resolved and settings.autonomy_auto_apply_enabled:
                        try:
                            apply_result = await _maybe_auto_apply(
                                anomaly, trigger_ctx, fix_branch, base_sha,
                            )
                        except Exception:
                            log.exception(
                                "reversible_apply failed for user=%s branch=%s",
                                user_id, fix_branch,
                            )
                            result_text += (
                                "\n\n[AUTO-APPLY — rawos.kernel.reversible_apply raised an "
                                "exception, see rawos logs. Fix remains propose-only.]"
                            )
                        else:
                            if apply_result is not None:
                                result_text += (
                                    f"\n\n[AUTO-APPLY — rawos.kernel.reversible_apply]\n"
                                    f"applied={apply_result.applied} healthy={apply_result.healthy} "
                                    f"rolled_back={apply_result.rolled_back}\n{apply_result.detail}"
                                )
                except asyncio.TimeoutError:
                    log.warning(
                        "anomaly_verifier timeout (%ds) for user=%s branch=%s",
                        VERIFICATION_TIMEOUT_S, user_id, fix_branch,
                    )
                    result_text += (
                        "\n\n[INDEPENDENT VERIFICATION — timed out after "
                        f"{VERIFICATION_TIMEOUT_S}s. Treat fix as UNVERIFIED.]"
                    )
                except Exception:
                    log.exception(
                        "anomaly_verifier failed for user=%s branch=%s", user_id, fix_branch,
                    )
                    result_text += (
                        "\n\n[INDEPENDENT VERIFICATION — verifier raised an "
                        "exception, see rawos logs. Treat fix as UNVERIFIED.]"
                    )

        return result_text
    finally:
        if scan_worktree:
            await remove_worktree(scan_worktree)

async def _run_proactive_agent(
    user_id: str,
    intent_obj: InferredIntent,
    timeliness_score: float = 1.0,
    timing_signals_json: str | None = None,
    trigger_type: str | None = None,
    trigger_ctx: dict[str, Any] | None = None,
    workdir_override: str | None = None,
) -> None:
    from rawos.manifester.writer import manifest_agent_result

    # PATCHED_LOG_INFERENCE_FIRST
    project_id, workdir = _get_user_project(user_id)
    if workdir_override:
        workdir = workdir_override
    if not project_id and user_id == RAWOS_ENTITY_USER_ID:
        project_id = RAWOS_ENTITY_PROJECT_ID

    # Always log the inference for research data — even if no project yet.
    # This ensures inference_log captures every intent cycle.
    inference_id = log_inference(
        user_id=user_id,
        goal=intent_obj.goal,
        domain=intent_obj.domain,
        confidence=intent_obj.confidence,
        source=intent_obj.source,
        timeliness_score=timeliness_score,
        timing_signals=timing_signals_json,
    )
    monitoring.inference_total.labels(source=intent_obj.source, domain=intent_obj.domain).inc()

    if not project_id or not workdir:
        log.info("no project for user=%s — inference logged (id=%s), skipping artifact", user_id, inference_id)
        return

    # Self-evaluation: check domain performance history before running agent
    _domain_conf = _evaluate_domain_confidence(user_id, intent_obj.domain)
    if _domain_conf < CONFIDENCE_THRESHOLD:
        _log_episodic(
            user_id, trigger_type or "", intent_obj.domain, intent_obj.goal,
            "silence",
            f"self-suppressed: domain_confidence={_domain_conf:.2f}",
            repo_root=workdir,
            project_id=project_id or "",
        )
        log.info(
            "rawos: self-suppressed user=%s domain=%s conf=%.2f",
            user_id, intent_obj.domain, _domain_conf,
        )
        return

    # Phase 15: probe watched repos before firing agent on entity user.
    # STUCK / ambient trigger with no specific trigger context — must find real evidence.
    _entity_probe: dict | None = None
    if (
        user_id == RAWOS_ENTITY_USER_ID
        and trigger_type not in ("SERVER_SCAN", "NEEDS_ATTENTION")
        and not (trigger_ctx or {}).get("diff_hunk")
    ):
        _entity_probe = await _select_entity_probe_target(user_id)
        if _entity_probe is None:
            _log_episodic(
                user_id, trigger_type or "", intent_obj.domain, intent_obj.goal,
                "silence", "entity-probe: no watched repo with recent activity",
                project_id=project_id or "",
            )
            log.info("rawos entity-probe: no target — SILENCE user=%s", user_id)
            return
        workdir = _entity_probe["workdir"]
        log.info(
            "rawos entity-probe: target=%s has_failures=%s user=%s",
            workdir, _entity_probe["has_failures"], user_id,
        )

    actions_str = "; ".join(intent_obj.suggested_actions) if intent_obj.suggested_actions else "analyze and summarize"
    message = (
        f"[Proactive analysis — {intent_obj.domain}]\n"
        f"User goal inferred: {intent_obj.goal}\n\n"
        f"Without waiting to be asked, perform: {actions_str}. "
        f"Be precise and actionable. Write findings as structured analysis."
    )

    log.info("proactive agent: user=%s goal='%s' conf=%.2f",
             user_id, intent_obj.goal, intent_obj.confidence)

    # Minimal DB records so proactive tasks appear in history
    intent_rec = Intent(
        user_id=user_id, project_id=project_id,
        raw_text=message, status=IntentStatus.EXECUTING,
    )
    db.create_intent(intent_rec)
    agent_rec = Agent(
        user_id=user_id, project_id=project_id,
        goal=f"[proactive] {intent_obj.goal[:180]}",
        model=settings.deepseek_model_pro,
    )
    agent_rec = agent_rec.transition(AgentStatus.ACTIVE)
    db.create_agent(agent_rec)

    # DIRECT_LLM_PROACTIVE: bypass orchestrator, direct DeepSeek API call, no tools.
    # Tools cause DSML markup in output; without tools the model writes pure markdown.
    from rawos.context.user_model import get_user_model
    import httpx

    user_model_data = get_user_model(user_id) or {}
    stack   = user_model_data.get("inferred_stack") or []
    domains = user_model_data.get("active_domains") or []
    recent  = user_model_data.get("recent_activity") or []

    recent_lines: list[str] = []
    for ev in recent[:12]:
        etype = ev.get("type", "")
        if etype == "file_write":
            recent_lines.append("  edited: " + ev.get("file", "") + " (" + ev.get("ext", "") + ")")
        elif etype == "file_delete":
            recent_lines.append("  deleted: " + ev.get("file", ""))
        elif etype == "intent_sent":
            recent_lines.append("  user typed: \"" + ev.get("preview", "") + "\"")

    # Build trigger-specific context block — this is the key signal that makes
    # artifacts specific rather than generic.
    trigger_block = ""
    tc = trigger_ctx or {}
    if _entity_probe:
        _ep_workdir = _entity_probe["workdir"]
        _ep_commits = _entity_probe["commits"] or "(none)"
        trigger_block = (
            "\n[TRIGGER: ENTITY PROBE — rawos autonomous scan]\n"
            f"Target: {_ep_workdir}\n"
            f"\nRecent commits:\n{_ep_commits}\n"
        )
        if _entity_probe["diff_stat"]:
            _ep_diff = _entity_probe["diff_stat"]
            trigger_block += f"\nLast commit diff:\n{_ep_diff}\n"
        if _entity_probe["test_output"]:
            _ep_status = "FAILURES FOUND" if _entity_probe["has_failures"] else "all tests passed"
            _ep_tout = _entity_probe["test_output"]
            trigger_block += f"\nTest status: {_ep_status}\n{_ep_tout}\n"
        if _entity_probe["has_failures"]:
            trigger_block += (
                "\nMission: the test failures above are your ONLY task.\n"
                "Fix the root cause. Do NOT write reports or diagnostic summaries.\n"
                "If you cannot fix it, SILENCE. Do not SIGNAL without a concrete reason.\n"
            )
        else:
            trigger_block += (
                "\nTask: identify ONE concrete fixable issue from the above. "
                "Nothing specific to fix? Choose SILENCE.\n"
            )
    elif trigger_type == "STUCK":
        trigger_block = (
            f"\n[TRIGGER: STUCK]\n"
            f"File: {tc.get('file', 'unknown')}\n"
            f"Edits in last {tc.get('duration_min', '?')} minutes: {tc.get('edit_count', '?')}\n"
            f"What changed: {tc.get('diff_summary', 'no diff available')}\n"
        )
        if tc.get("diff_hunk"):
            trigger_block += f"Diff excerpt:\n{tc['diff_hunk'][:400]}\n"
    elif trigger_type == "JUST_FINISHED":
        trigger_block = (
            f"\n[TRIGGER: JUST_FINISHED — git commit detected]\n"
            f"Recent commits:\n{tc.get('recent_commits', '')}\n"
            f"Files changed:\n{tc.get('files_changed', '')}\n"
        )
    elif trigger_type == "IDLE_OPPORTUNITY":
        trigger_block = (
            f"\n[TRIGGER: IDLE — {tc.get('idle_minutes', '?')} min quiet after "
            f"{tc.get('edits_in_session', '?')} file edits]\n"
        )
    elif trigger_type == "NEEDS_ATTENTION":
        import datetime as _dt
        _sf = _dt.datetime.fromtimestamp(tc.get("start_ts", 0)).strftime("%H:%M")
        _at = ", ".join(tc.get("attendees", [])) or "none listed"
        trigger_block = (
            "[TRIGGER: NEEDS_ATTENTION]\n"
            f"Event: {tc.get('title', 'Untitled')} | Time: {_sf}"
            f" | In: {tc.get('minutes_away', '?'):.0f} min\n"
            f"Attendees: {_at}\n"
            f"Location: {tc.get('location', '') or 'not specified'}\n"
        )
    elif trigger_type == "SERVER_SCAN":
        trigger_block = (
            f"\n[SERVER_SCAN — autonomous rawos observation]\n"
            f"Anomaly type: {tc.get('anomaly_kind', 'unknown')}\n"
            f"Detail: {tc.get('anomaly_detail', 'unknown')}\n"
        )
        if tc.get("service"):
            trigger_block += f"Service: {tc['service']}\n"
        trigger_block += f"Severity: {tc.get('severity', 0)}/10\n"
        if tc.get("last_log"):
            trigger_block += f"\nRecent logs:\n{tc['last_log'][:600]}\n"

    if _entity_probe and _entity_probe["has_failures"]:
        _probe_target = _entity_probe["workdir"]
        context_summary = (
            f"Mission: fix test failures in {_probe_target}\n"
            "This is not a debugging analysis task. You must fix the failing tests.\n"
            + trigger_block
        )
    else:
        context_summary = (
            "Tech stack: " + (", ".join(stack) if stack else "unknown") + "\n"
            + "Active domains: " + (", ".join(domains) if domains else "none detected") + "\n"
            + "Inferred intent: " + intent_obj.goal
            + " (confidence=" + f"{intent_obj.confidence:.2f}"
            + ", domain=" + intent_obj.domain + ")\n"
            + "Suggested actions: " + "; ".join(intent_obj.suggested_actions or []) + "\n"
            + trigger_block
            + "Recent activity:\n"
            + ("\n".join(recent_lines) if recent_lines else "  (no recent file activity)")
        )

    # Append deep code context for STUCK and JUST_FINISHED
    if trigger_type in ("STUCK", "JUST_FINISHED"):
        try:
            from rawos.kernel.context_reader import read_code_context, format_for_prompt
            code_ctx = await read_code_context(trigger_type, trigger_ctx or {}, workdir)
            if code_ctx:
                context_summary += format_for_prompt(code_ctx)
        except Exception:
            log.debug("context_reader failed (non-fatal): user=%s", user_id)

    db.update_intent(user_id, intent_rec.id, status=IntentStatus.EXECUTING)

    _agent_loop_start_ts = int(time.time())

    # Compute stable cooldown key for this agent run
    _run_cooldown_key = _compute_cooldown_key(trigger_type, intent_obj.domain, trigger_ctx)

    result_text = await _run_proactive_loop(
        user_id=user_id,
        agent_rec=agent_rec,
        intent_rec=intent_rec,
        intent_obj=intent_obj,
        context_summary=context_summary,
        trigger_ctx=trigger_ctx,
        trigger_type=trigger_type,
    )
    if result_text is None:
        return
    if len(result_text) < 80:
        log.debug("proactive: result too short (%d chars) — skipping", len(result_text))
        return

    # Parse entity decision from first word of response
    _first = result_text.strip().split()[0].upper() if result_text.strip() else "SILENCE"
    _decision = _first if _first in ("CONTRIBUTE", "SIGNAL", "SILENCE") else "SILENCE"
    _confidence = _parse_confidence(result_text)

    _episodic_id = _log_episodic(
        user_id, trigger_type or "", intent_obj.domain, intent_obj.goal,
        _decision.lower(),
        result_text[:500] if _decision != "SILENCE" else None,
        repo_root=workdir,
        self_confidence=_confidence,
        project_id=project_id or "",
    )

    if _decision == "SILENCE":
        log.info("rawos: SILENCE for user=%s domain=%s", user_id, intent_obj.domain)
        _record_proactive_artifact(
            user_id, intent_obj.goal, _confidence,
            "", None, None,
            action_type="silence",
            cooldown_key=_run_cooldown_key,
        )
        return
    if _decision == "CONTRIBUTE" and _confidence < CONFIDENCE_THRESHOLD:
        log.info(
            "rawos: CONTRIBUTE suppressed conf=%.2f < %.2f for user=%s domain=%s",
            _confidence, CONFIDENCE_THRESHOLD, user_id, intent_obj.domain,
        )
        return

    if _decision == "CONTRIBUTE":
        # Output IS the git commit — no .md file written
        db.save_memory(Memory(
            user_id=user_id, project_id=project_id, agent_id=agent_rec.id,
            tier=MemoryTier.EPISODIC, role=MessageRole.ASSISTANT, content=result_text,
        ))
        # Agent verifies inline per system prompt — parse result synchronously.
        # No human gate: rawos is accountable for its own outcome.
        _verification = _parse_verification_result(result_text)
        if _verification and _episodic_id:
            _update_episodic_outcome(_episodic_id, _verification)
            log.info(
                "rawos CONTRIBUTE: outcome=%s user=%s domain=%s",
                _verification, user_id, intent_obj.domain,
            )
        elif _episodic_id and workdir:
            # Fallback: agent did not self-report, run tests async (best-effort)
            asyncio.create_task(_post_commit_self_rate(_episodic_id, workdir))
        return

    # SIGNAL: write manifest file
    # Save assistant response to memory
    db.save_memory(Memory(
        user_id=user_id, project_id=project_id, agent_id=agent_rec.id,
        tier=MemoryTier.EPISODIC, role=MessageRole.ASSISTANT, content=result_text,
    ))

    try:
        file_path, artifact_id_out = await manifest_agent_result(
            user_id=user_id, project_id=project_id, workdir=workdir,
            goal=intent_obj.goal, domain=intent_obj.domain, content=result_text,
            target_dir=_manifest_target(workdir, trigger_type),
        )
        _manifest_action_type = "attention" if trigger_type == "NEEDS_ATTENTION" else "analysis"
        pa_id = _record_proactive_artifact(
            user_id, intent_obj.goal, intent_obj.confidence,
            file_path, artifact_id_out, agent_rec.id,
            action_type=_manifest_action_type,
            cooldown_key=_run_cooldown_key,
        )
        # Link inference to artifact so ratings can mark it correct/incorrect
        if pa_id:
            link_inference_to_artifact(inference_id, pa_id)
            # Back-fill artifact_id on all tool calls from this agent run
            with db._conn() as _link_conn:
                _link_conn.execute(
                    "UPDATE proactive_tool_calls SET artifact_id = ? "
                    "WHERE user_id = ? AND artifact_id IS NULL AND called_at >= ?",
                    (pa_id, user_id, _agent_loop_start_ts),
                )
            from rawos.context.collector import increment_rawos_action
            increment_rawos_action(user_id)
            # Push notification to mobile devices (fire-and-forget)
            _PUSH_TITLES = {
                "STUCK":            "rawos — suggestion ready",
                "JUST_FINISHED":    "rawos — session summary",
                "NEEDS_ATTENTION":  "rawos — attention needed",
                "IDLE_OPPORTUNITY": "rawos — insight",
            }
            try:
                from rawos.push.service import send_push_to_user
                asyncio.create_task(send_push_to_user(
                    user_id=user_id,
                    title=_PUSH_TITLES.get(trigger_type, "rawos"),
                    body=intent_obj.goal[:200],
                    data={
                        "artifact_id":  pa_id,
                        "action_type":  _manifest_action_type,
                        "goal":         intent_obj.goal[:200],
                    },
                ))
            except Exception:
                log.exception("push schedule failed (non-fatal)")
        log.info("proactive manifest: %s", file_path)

        # Level 1 — code fix: only on STUCK trigger with earned autonomy
        if (
            trigger_type == "STUCK"
            and trigger_ctx
            and _get_user_autonomy_level(user_id, "analysis") >= 1
        ):
            await _generate_code_fix(
                user_id=user_id,
                project_id=project_id,
                workdir=workdir,
                agent_id=agent_rec.id,
                intent_obj=intent_obj,
                trigger_ctx=trigger_ctx,
                analysis_text=result_text,
            )
    except Exception:
        log.exception("manifester failed for user=%s", user_id)


def _is_autonomous_cooldown(anomaly_kind: str) -> bool:
    """Check if this anomaly type was recently acted on by autonomous scan."""
    cutoff = int(time.time()) - AUTONOMOUS_SCAN_COOLDOWN_S
    with db._conn() as conn:
        row = conn.execute(
            """SELECT 1 FROM episodic_memory
               WHERE user_id = ? AND trigger_type = 'SERVER_SCAN'
               AND domain = ? AND ts >= ? LIMIT 1""",
            (RAWOS_ENTITY_USER_ID, anomaly_kind, cutoff),
        ).fetchone()
    return row is not None


async def _update_earned_autonomy_track_records(snapshot) -> None:
    """Stage 3 (observational): advance the earned-autonomy ladder.

    For every (repo_root, anomaly_domain) rawos has previously proposed a
    rawos/fix-* branch for (rawos_commits.repo_root/anomaly_domain), check
    whether a human has merged that branch (is_branch_merged) and whether
    the anomaly is currently present in , then advance that
    class's autonomy_track_record. A class only graduates after
    GRADUATION_THRESHOLD merged-and-stayed-resolved cycles
    (rawos.kernel.track_record) — this function never auto-applies
    anything, it only records outcomes. Read-only with respect to the
    scanned repos (git rev-parse/merge-base only); never the live rawos
    tree.
    """
    present = {
        (anomaly.affected_path, anomaly.domain)
        for anomaly in snapshot.actionable
        if anomaly.severity >= AUTONOMOUS_SCAN_THRESHOLD
    }
    now = int(time.time())
    with db._conn() as conn:
        rows = conn.execute(
            """SELECT repo_root, anomaly_domain, branch, commit_hash FROM (
                   SELECT repo_root, anomaly_domain, branch, commit_hash,
                          ROW_NUMBER() OVER (
                              PARTITION BY repo_root, anomaly_domain
                              ORDER BY created_at DESC
                          ) AS rn
                   FROM rawos_commits
                   WHERE user_id = ? AND repo_root IS NOT NULL
                     AND anomaly_domain IS NOT NULL
                     AND branch LIKE 'rawos/fix-%'
               ) WHERE rn = 1""",
            (RAWOS_ENTITY_USER_ID,),
        ).fetchall()

    for repo_root, anomaly_domain, branch, sha in rows:
        if not Path(repo_root).is_dir():
            continue
        if not await is_branch_merged(repo_root, sha):
            continue
        update_track_record(
            RAWOS_ENTITY_USER_ID, repo_root, anomaly_domain,
            anomaly_present=(repo_root, anomaly_domain) in present,
            branch_merged=True,
            fix_branch=branch, fix_sha=sha, now=now,
        )


async def _run_autonomous_scan() -> None:
    """
    rawos examines the entire server. No human trigger.
    Finds the highest-severity actionable anomaly and acts on it.
    One action per scan cycle — prioritized by severity.
    """
    loop = asyncio.get_event_loop()
    try:
        from rawos.context.server_scanner import collect_server_state
        snapshot = await asyncio.wait_for(
            loop.run_in_executor(None, collect_server_state),
            timeout=30.0,
        )
    except Exception:
        log.exception("server_scanner failed")
        return

    try:
        await _update_earned_autonomy_track_records(snapshot)
    except Exception:
        log.exception("autonomy track-record update failed")

    if snapshot.max_severity < AUTONOMOUS_SCAN_THRESHOLD:
        return  # server is healthy — rawos is silent

    for anomaly in snapshot.actionable:
        if anomaly.severity < AUTONOMOUS_SCAN_THRESHOLD:
            break
        _anomaly_domain = anomaly.domain
        if _is_autonomous_cooldown(_anomaly_domain):
            log.debug("autonomous: anomaly on cooldown domain=%s", _anomaly_domain)
            continue

        log.info(
            "rawos autonomous: anomaly=%s severity=%d service=%s",
            anomaly.kind, anomaly.severity, anomaly.service or "N/A",
        )

        intent_obj = InferredIntent(
            goal=anomaly.detail[:200],
            domain=_anomaly_domain,
            confidence=0.92,
            source="server_scan",
            suggested_actions=[
                f"investigate {anomaly.kind}",
                "read relevant logs",
                "diagnose root cause and fix if possible",
            ],
        )

        trigger_ctx = anomaly.to_trigger_ctx()

        await _run_proactive_agent(
            user_id=RAWOS_ENTITY_USER_ID,
            intent_obj=intent_obj,
            trigger_type="SERVER_SCAN",
            trigger_ctx=trigger_ctx,
            workdir_override=anomaly.affected_path,
        )
        break  # one action per scan cycle — act on highest priority first


async def autonomous_server_scan_loop() -> None:
    """
    rawos autonomous attention loop.
    Runs every settings.autonomous_scan_interval_s seconds. No human activity required.
    This is the inversion: rawos acts because it found something,
    not because a human triggered it.
    """
    log.info(
        "rawos autonomous scan started (interval=%ds, threshold=%d/10)",
        settings.autonomous_scan_interval_s, AUTONOMOUS_SCAN_THRESHOLD,
    )
    while True:
        try:
            await _run_autonomous_scan()
        except asyncio.CancelledError:
            log.info("rawos autonomous scan cancelled")
            break
        except Exception:
            log.exception("autonomous scan error (continuing)")
        await asyncio.sleep(settings.autonomous_scan_interval_s)


async def proactive_scan_loop() -> None:
    log.info("proactive scheduler started (interval=%.0fs, threshold=%.2f)",
             SCAN_INTERVAL_S, CONFIDENCE_THRESHOLD)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_PROACTIVE)
    while True:
        try:
            await asyncio.sleep(SCAN_INTERVAL_S)
            await _scan_once(semaphore)
        except asyncio.CancelledError:
            log.info("proactive scheduler cancelled")
            break
        except Exception:
            log.exception("proactive scan error (continuing)")


async def _scan_once(semaphore: asyncio.Semaphore) -> None:
    user_ids = _get_active_users()
    if not user_ids:
        return
    log.debug("proactive scan: %d active users", len(user_ids))
    tasks = []

    for uid in user_ids:
        # ── Semantic trigger detection (replaces pure confidence threshold) ──
        trigger_type, trigger_ctx = _detect_trigger(uid)

        intent_obj = await infer_intent(uid)
        if not intent_obj.goal:
            continue

        if trigger_type in ("JUST_FINISHED", "NEEDS_ATTENTION"):
            # Always fire — clear, high-value semantic signals.
            if trigger_type == "JUST_FINISHED":
                log.info("proactive JUST_FINISHED: user=%s commit=%s",
                         uid, trigger_ctx.get("commit_hash", "?"))
            else:
                log.info(
                    "proactive NEEDS_ATTENTION: user=%s event=%r in %.0fmin",
                    uid, trigger_ctx.get("title", "?"),
                    trigger_ctx.get("minutes_away", 0),
                )

        elif trigger_type in ("STUCK", "IDLE_OPPORTUNITY"):
            # Fire if confidence OK — semantic trigger already confirmed relevance.
            if intent_obj.confidence < CONFIDENCE_THRESHOLD:
                log.debug("trigger=%s but confidence=%.2f < %.2f — skip",
                          trigger_type, intent_obj.confidence, CONFIDENCE_THRESHOLD)
                continue
            log.info("proactive %s trigger: user=%s edits=%s",
                     trigger_type, uid,
                     trigger_ctx.get("edit_count", trigger_ctx.get("edits_in_session", "?")))

        else:
            # No semantic trigger — use confidence + timeliness gates (legacy path)
            if intent_obj.confidence < CONFIDENCE_THRESHOLD:
                continue
            from rawos.timing.model import get_timeliness
            timing_result = await asyncio.get_event_loop().run_in_executor(
                None, get_timeliness, uid, intent_obj.domain
            )
            if not timing_result.fallback_mode and timing_result.timeliness_score < TIMELINESS_THRESHOLD:
                log.debug("timing gate: user=%s score=%.2f — skip", uid, timing_result.timeliness_score)
                continue

        # NEEDS_ATTENTION: cooldown per event UID to prevent re-firing
        cooldown_key = _compute_cooldown_key(trigger_type, intent_obj.domain, trigger_ctx)
        if _is_goal_on_cooldown(uid, cooldown_key):
            log.debug("goal on cooldown: user=%s key='%s'", uid, cooldown_key[:60])
            continue

        # Get timeliness score for logging (fallback to 1.0 when trigger bypasses gate)
        try:
            from rawos.timing.model import get_timeliness
            tr = await asyncio.get_event_loop().run_in_executor(
                None, get_timeliness, uid, intent_obj.domain
            )
            t_score = tr.timeliness_score
            t_json  = tr.to_json()
        except Exception:
            t_score, t_json = 1.0, None

        async def _bounded(
            u: str = uid,
            i: InferredIntent = intent_obj,
            ts: float = t_score,
            tj: str | None = t_json,
            tt: str | None = trigger_type,
            tc: dict = trigger_ctx,
        ) -> None:
            async with semaphore:
                await _run_proactive_agent(
                    u, i,
                    timeliness_score=ts,
                    timing_signals_json=tj,
                    trigger_type=tt,
                    trigger_ctx=tc,
                )

        tasks.append(asyncio.create_task(_bounded()))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def rawos_self_probe_loop() -> None:
    """
    Phase 16 self-modification entry point.

    DORMANT by default (settings.self_probe_enabled = False, commit 552b752e)
    — see PLAN.md "Phase 16 — Pass 2 — IMPLEMENTED (2026-06-09)". While
    disabled,
    this logs once and returns immediately: no loop, no sleep, no side
    effects. A human must flip settings.self_probe_enabled to True after
    observing one manual self-probe worktree cycle.

    When enabled, this is meant to run every SELF_PROBE_INTERVAL_S, always
    against an isolated `git worktree` of /root/rawos (never the live
    working tree — see _targets_rawos_own_repo / TIER enforcement in
    rawos/kernel/tools.py), producing rawos/self-improve-* branches for
    human review. NO auto-merge, NO auto-restart.
    """
    if not settings.self_probe_enabled:
        log.info("rawos self-probe loop disabled (settings.self_probe_enabled=False) — not starting")
        return

    log.info("rawos self-probe loop started (interval=%ds)", SELF_PROBE_INTERVAL_S)
    while True:
        try:
            await _run_self_probe_cycle()
        except asyncio.CancelledError:
            log.info("rawos self-probe loop cancelled")
            break
        except Exception:
            log.exception("self-probe cycle error (continuing)")
        await asyncio.sleep(SELF_PROBE_INTERVAL_S)


async def _run_self_probe_cycle() -> None:
    """Phase 16 — self-probe worktree cycle.

    Creates an isolated git worktree of _SELF_PROBE_RAWOS_REPO on a fresh
    rawos/self-improve-<timestamp> branch, runs the rawos agent loop inside
    it (TIER enforcement active), and leaves results on the branch for human
    review.  Cleans up the worktree on exit whether or not the agent
    succeeded.

    Invariants (never violated):
    - workdir passed to agent_loop is always the worktree path, never
      _SELF_PROBE_RAWOS_REPO.
    - Branch name always matches rawos/self-improve-<timestamp>.
    - No auto-merge, no auto-restart of rawos.service.
    - Origin HEAD (master) is not moved by this cycle.
    """
    import time as _time

    timestamp   = int(_time.time())
    branch_name = f"rawos/self-improve-{timestamp}"

    worktree_path = await create_worktree(_SELF_PROBE_RAWOS_REPO)
    if not worktree_path:
        log.error(
            "self-probe: worktree creation failed for %s — aborting cycle",
            _SELF_PROBE_RAWOS_REPO,
        )
        return

    try:
        # create_worktree leaves a detached HEAD; name it before the agent runs.
        br = await run_bash(f"git checkout -b '{branch_name}'", worktree_path)
        if br.exit_code != 0:
            log.error(
                "self-probe: git checkout -b %s failed: %s",
                branch_name, br.stderr[:300],
            )
            return

        log.info(
            "self-probe: cycle start — worktree=%s branch=%s",
            worktree_path, branch_name,
        )

        goal = (
            "CONTRIBUTE.\n"
            "Create a new file: tests/test_tier1_remaining_prefixes.py\n"
            "The file must contain a pytest class TestTier1RemainingPrefixes with\n"
            "three test methods that verify _in_tier1_allowlist() returns True for:\n"
            "  - rawos/dataset/schema.py\n"
            "  - rawos/study/notes.md\n"
            "  - rawos/timing/benchmark.py\n"
            "Import only from rawos.kernel.tools. No other imports needed.\n"
            "These three paths are in _TIER1_PREFIXES (rawos/kernel/tools.py)\n"
            "but have no existing tests.\n\n"
            "Steps:\n"
            "1. write_file path=tests/test_tier1_remaining_prefixes.py\n"
            "2. bash_readonly cmd='python -m pytest "
            "tests/test_tier1_remaining_prefixes.py -x -q 2>&1 | tail -5'\n"
            "3. git_commit message='rawos: add TIER1 allowlist tests for "
            "dataset/study/timing\\n\\nSelf-probe: _in_tier1_allowlist had no "
            "assertions for rawos/dataset/, rawos/study/, rawos/timing/ despite "
            "all three in _TIER1_PREFIXES.\\nConfidence: 0.95'\n"
            "Stop after step 3. No further exploration."
        )

        # Minimal DB records for billing attribution and audit trail.
        intent_rec = Intent(
            user_id=RAWOS_ENTITY_USER_ID,
            project_id=RAWOS_ENTITY_PROJECT_ID,
            raw_text=goal,
            status=IntentStatus.EXECUTING,
        )
        db.create_intent(intent_rec)
        agent_rec = Agent(
            user_id=RAWOS_ENTITY_USER_ID,
            project_id=RAWOS_ENTITY_PROJECT_ID,
            goal=f"[self-probe] {goal[:180]}",
            model=settings.deepseek_model_pro,
        )
        agent_rec = agent_rec.transition(AgentStatus.ACTIVE)
        db.create_agent(agent_rec)

        messages = [{"role": "user", "content": goal}]

        async def _drain() -> None:
            async for event in agent_loop.run(
                messages=messages,
                workdir=worktree_path,
                model=settings.deepseek_model_pro,
                intent_id=intent_rec.id,
                user_id=RAWOS_ENTITY_USER_ID,
                system_prompt=_SELF_PROBE_SYSTEM_PROMPT,
                tool_definitions=_get_tools_for_self_probe(),
                agent_id=agent_rec.id,
                event_type="self_probe",
            ):
                if event.get("type") == "error":
                    log.error("self-probe agent error: %s", event.get("message"))

        try:
            await asyncio.wait_for(_drain(), timeout=MAX_PROACTIVE_LOOP_TIME_S)
            db.update_intent(
                RAWOS_ENTITY_USER_ID, intent_rec.id, status=IntentStatus.COMPLETED
            )
        except asyncio.TimeoutError:
            log.warning(
                "self-probe: agent timed out after %ds", MAX_PROACTIVE_LOOP_TIME_S
            )
            db.update_intent(
                RAWOS_ENTITY_USER_ID, intent_rec.id, status=IntentStatus.FAILED
            )
        except Exception:
            log.exception("self-probe: agent loop error")
            db.update_intent(
                RAWOS_ENTITY_USER_ID, intent_rec.id, status=IntentStatus.FAILED
            )

        log.info(
            "self-probe: cycle complete — branch=%s available for human review",
            branch_name,
        )

    finally:
        await remove_worktree(worktree_path)
        log.info("self-probe: worktree cleaned up — %s", worktree_path)


# ---------------------------------------------------------------------------
# Seam B — Narrative consolidation: being writes its own continuous self-narrative
# ---------------------------------------------------------------------------

async def _run_narrative_consolidation_cycle() -> None:
    """One consolidation cycle: read recent autonomous episodic history,
    call write_self_narrative, persist result.

    Non-fatal: any exception is logged and swallowed — the loop must survive.
    """
    try:
        prior = db.get_self_narrative(RAWOS_ENTITY_USER_ID) or ""
        with db._conn() as conn:
            rows = conn.execute(
                """SELECT trigger_type, domain, inferred_goal, decision,
                          action_summary, outcome, self_confidence, ts
                   FROM episodic_memory
                   WHERE user_id = ?
                   ORDER BY ts DESC
                   LIMIT 40""",
                (RAWOS_ENTITY_USER_ID,),
            ).fetchall()
        episodic_history = [dict(r) for r in rows]
        from rawos.context.user_model import get_user_model as _get_user_model
        user_model_row = _get_user_model(RAWOS_ENTITY_USER_ID) or {}
        new_narrative = await write_self_narrative(
            prior, user_model_row, episodic_history
        )
        if new_narrative:
            db.set_self_narrative(RAWOS_ENTITY_USER_ID, new_narrative)
            log.info("narrative_consolidation: narrative updated (%d chars)", len(new_narrative))
        else:
            log.info("narrative_consolidation: LLM returned empty — prior preserved")
    except Exception:
        log.exception("narrative_consolidation: cycle error (non-fatal)")


async def rawos_narrative_consolidation_loop() -> None:
    """Periodic loop that consolidates the being's autonomous episodic history
    into a coherent self-narrative.

    Gated by settings.narrative_consolidation_enabled (default False — activate
    once Seam A has accumulated sufficient episodic history).
    """
    if not settings.narrative_consolidation_enabled:
        log.info("narrative_consolidation: disabled — loop exits immediately")
        return
    interval = settings.narrative_consolidation_interval_s
    log.info("narrative_consolidation: loop started (interval=%ds)", interval)
    while True:
        await _run_narrative_consolidation_cycle()
        await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# Milestone 6 — Autonomous Operator Loop
#
# Wires the proven kernel/operator.py::operate_on_file gate into the proactive
# scheduler so the being autonomously detects config drift on its owner-
# allowlisted managed_file_targets (via each target's validator command as an
# unfakeable detection oracle) and proposes/applies reversible fixes.
#
# CRITICAL IDENTITY INVARIANT: unlike the git autonomous scan (which runs as
# RAWOS_ENTITY_USER_ID), the operator allowlist (managed_file_targets) and its
# graduation ledger (operator_track_record) are OWNER-keyed — the same owner
# identity the chat manage_file path uses (db.get_user_by_email(
# settings.telegram_owner_email)). This loop MUST run as owner.id. Empty
# telegram_owner_email or no matching user row => no-op (cannot invent an
# owner).
# ---------------------------------------------------------------------------

_CONFIG_FIX_SYSTEM_PROMPT = """You are rawos, an autonomous machine operator repairing a broken managed configuration file on the host you run on.

You will be given the file's path, its current content, and the error output from the file's validator command (the command that proves the file is broken).

Respond with ONLY the complete corrected file content — no explanation, no commentary, no markdown code fences. Make the minimal change needed to make the validator pass while preserving the file's existing structure, style, and intent."""


async def _generate_config_fix(
    target_path: str, current_content: bytes, validator_error: str,
) -> bytes | None:
    """Ask the LLM for a corrected version of a broken managed config file.

    Mirrors _generate_code_fix's "never propose a no-op" discipline: returns
    None (no proposal) when the LLM output is empty, identical to the current
    content, or the LLM call itself fails. Never raises.
    """
    current_text = current_content.decode("utf-8", errors="replace")
    user_text = (
        f"File path: {target_path}\n\n"
        f"Validator error output:\n{validator_error}\n\n"
        f"Current file content:\n{current_text}"
    )

    try:
        raw_result = await summarizer._complete(_CONFIG_FIX_SYSTEM_PROMPT, user_text)
    except Exception:
        log.exception("_generate_config_fix: LLM call failed for target=%s", target_path)
        return None

    corrected_text = _strip_code_fences(raw_result.strip())
    if not corrected_text.strip():
        log.debug("_generate_config_fix: empty LLM output — skipping target=%s", target_path)
        return None

    if corrected_text.strip() == current_text.strip():
        log.debug("_generate_config_fix: LLM output identical to current — skipping target=%s", target_path)
        return None

    return corrected_text.encode("utf-8")


def _is_operator_cooldown(target_path: str) -> bool:
    """Check whether target_path was scanned by the operator loop recently.

    Mirrors _is_autonomous_cooldown but keyed by trigger_type='OPERATOR_SCAN'
    and domain=target_path (not by user_id — managed_file_targets is a
    single-owner allowlist, so per-target is sufficient).
    """
    cutoff = int(time.time()) - OPERATOR_SCAN_COOLDOWN_S
    with db._conn() as conn:
        row = conn.execute(
            """SELECT 1 FROM episodic_memory
               WHERE trigger_type = 'OPERATOR_SCAN'
               AND domain = ? AND ts >= ? LIMIT 1""",
            (target_path, cutoff),
        ).fetchone()
    return row is not None


def _route_operator_outcome(owner_id: str, target_path: str, outcome) -> None:
    """Record an OperateOutcome from the autonomous operator scan.

    The loop adds no gating logic of its own — operate_on_file already decided
    auto-apply vs propose-only (and already performed capture/apply/verify/
    rollback for the auto-apply case). This function only manifests the
    decision as a proactive artifact + episodic record.

    auto_applied -> decision='contribute' (the being acted on the machine).
    proposed     -> decision='signal' (the being noticed drift and has a
                     verified-shape fix, but is not yet allowed to apply it —
                     the owner actions it via the existing chat manage_file
                     path; a durable autonomous-approve store is deferred,
                     stated as a known limitation, not faked here).
    """
    goal = f"repair managed file {target_path}"

    if outcome.auto_applied:
        result = outcome.operation_result
        if result is not None and result.verified:
            summary = f"auto-applied fix to {target_path} (validator passed)"
        elif result is not None and result.restored:
            summary = f"applied fix to {target_path} failed validation — rolled back to original"
        else:
            summary = f"auto-applied fix to {target_path}: {outcome.reason}"

        _log_episodic(owner_id, "OPERATOR_SCAN", target_path, goal, "contribute", summary)
        _record_proactive_artifact(
            owner_id, goal, 1.0, target_path, None, None,
            action_type="operator_apply", cooldown_key=target_path,
        )
    elif outcome.proposed:
        summary = f"proposed fix for {target_path} ({outcome.reason})"
        _log_episodic(owner_id, "OPERATOR_SCAN", target_path, goal, "signal", summary)
        _record_proactive_artifact(
            owner_id, goal, 0.5, target_path, None, None,
            action_type="operator_proposal", cooldown_key=target_path,
        )


async def _run_operator_scan_cycle() -> None:
    """One autonomous operator scan cycle.

    Resolves the owner via db.get_user_by_email(settings.telegram_owner_email)
    — the SAME identity resolution chat's manage_file path uses — and iterates
    that owner's managed_file_targets. For each target: run its validator
    command (the unfakeable detection oracle); a passing validator means the
    target is healthy (skip, no LLM call). A failing validator means the
    target is broken: if the target is on cooldown, skip (avoid hammering);
    otherwise read the current content, ask the LLM for a fix, and route it
    through operate_on_file(owner.id, ...) — the proven gate that decides
    auto-apply vs propose-only and performs capture/apply/verify/rollback.

    Never raises out of the loop: a refusal on one target (self-protection,
    e.g. the rawos unit file) is caught, logged, and the cycle continues with
    the remaining targets — per the explicit "refusal on one target does not
    block others" requirement.

    Stated deviation from "one action per cycle" (plan wording): every broken,
    non-cooldown target is acted on within the same cycle, each independently
    isolated by its own try/except. Serialization is per-target (the cooldown
    + graduation ledger are per-target), so concurrent multi-target action
    within one cycle does not corrupt any single target's rollback state.
    """
    if not settings.telegram_owner_email:
        log.debug("operator_scan: telegram_owner_email unset — no-op")
        return

    owner = db.get_user_by_email(settings.telegram_owner_email)
    if owner is None:
        log.debug("operator_scan: telegram_owner_email has no matching user — no-op")
        return

    for target in db.list_managed_file_targets(owner.id):
        target_path = target["target_path"]
        validator_cmd = target["validator_cmd"]

        validator_result = run_validator(validator_cmd)
        if validator_result.passed:
            continue  # healthy — nothing to do, no LLM call, no episodic spam

        if _is_operator_cooldown(target_path):
            log.debug("operator_scan: target on cooldown — target=%s", target_path)
            continue

        try:
            current_content = get_arch().file_operator.read(target_path)
            if current_content is None:
                log.warning("operator_scan: target unreadable — target=%s", target_path)
                continue

            fix_content = await _generate_config_fix(
                target_path, current_content, validator_result.output,
            )
            if fix_content is None:
                continue

            outcome = operate_on_file(owner.id, target_path, fix_content)
        except FileOperatorRefusalError:
            log.warning("operator_scan: refused self-protected target — target=%s", target_path)
            continue
        except Exception:
            log.exception("operator_scan: error scanning target=%s", target_path)
            continue

        _route_operator_outcome(owner.id, target_path, outcome)


async def rawos_operator_scan_loop() -> None:
    """Periodic loop: autonomously scan owner-allowlisted managed file targets
    and propose/apply reversible fixes via operate_on_file.

    Gated by settings.operator_scan_enabled (default False — dormant, zero
    behavior change until the owner opts in). Mirrors
    rawos_narrative_consolidation_loop's shape: try/except per cycle so the
    loop never dies, sleeps operator_scan_interval_s between cycles.
    """
    if not settings.operator_scan_enabled:
        log.info("operator_scan: disabled — loop exits immediately")
        return
    interval = settings.operator_scan_interval_s
    log.info("operator_scan: loop started (interval=%ds)", interval)
    while True:
        try:
            await _run_operator_scan_cycle()
        except Exception:
            log.exception("operator_scan: cycle error (non-fatal)")
        await asyncio.sleep(interval)
