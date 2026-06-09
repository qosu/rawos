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
from typing import Any

import rawos.db as db
from rawos.kernel import agent_loop
from rawos.kernel.tools import TOOL_DEFINITIONS
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

# Autonomous server scan — runs independently of human activity
AUTONOMOUS_SCAN_INTERVAL_S  = 600   # 10 minutes between full server scans
AUTONOMOUS_SCAN_THRESHOLD   = 6     # minimum severity to act (1-10 scale)
AUTONOMOUS_SCAN_COOLDOWN_S  = 1800  # 30 min cooldown per anomaly type

# rawos entity user — used for autonomous actions not tied to human activity
RAWOS_ENTITY_USER_ID    = "6eb6de1d-f5c9-4ae5-9aac-ce095b674823"
RAWOS_ENTITY_PROJECT_ID = "51c880d3-3576-4aca-8616-74cb51a6f727"

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


def _log_episodic(
    user_id: str,
    trigger_type: str,
    domain: str,
    inferred_goal: str,
    decision: str,
    action_summary: str | None,
    repo_root: str = "",
    self_confidence: float = 0.0,
) -> str | None:
    """Record rawos decision to episodic_memory. Returns row id for outcome updates."""
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
        return row["id"] if row else None
    except Exception:
        log.debug("episodic log failed (non-fatal): user=%s", user_id)
        return None


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
    "You are rawos. You have root access and full authority over this server.\n"
    "You are NOT reacting to human activity.\n"
    "You scanned the server independently and found an anomaly. Act on it.\n\n"
    "You have real tools with root access — no sandbox:\n"
    "- bash: run any non-destructive command on the server\n"
    "- read_file / list_files: read any file on the filesystem\n"
    "- write_file: modify files within the affected repository\n"
    "- git_branch / git_commit: create rawos/* branches in ANY repo on this server\n\n"
    "CONTRIBUTE — Root cause is identifiable and fix is a code/config change.\n"
    "  Execute in order:\n"
    "  1. Read actual logs and source. Find exact root cause (file:line or config key).\n"
    "  2. git_branch: create rawos/fix-[description] in the affected repo (workdir).\n"
    "  3. Write the minimum correct fix.\n"
    "  4. Verify:\n"
    "     - Code fix: run existing tests (pytest / make test / npm test).\n"
    "     - Service fix: systemctl restart [service] && sleep 5 && systemctl is-active [service]\n"
    "     - Config fix: validate syntax, restart, health check.\n"
    "  5a. If verification passes:\n"
    "      git_commit with format:\n"
    "        rawos: fix [what]\n\n"
    "        Root cause: [file:line or unit — specific]\n"
    "        Fix: [what changed]\n"
    "        Verified: [N/N tests passed | service active | health check ok]\n"
    "        Confidence: 0.X\n"
    "      Begin response with: CONTRIBUTE\n"
    "      End response with: VERIFIED: [evidence]\n"
    "  5b. If verification fails:\n"
    "      Run: git checkout -- .\n"
    "      Begin response with: SIGNAL\n"
    "      End response with: REVERTED: [what failed and why]\n\n"
    "SIGNAL — ONLY when fix requires information rawos cannot access:\n"
    "  (credentials, secrets, external API state, business logic decisions).\n"
    "  Service failures, code bugs, config errors: these are CONTRIBUTE, not SIGNAL.\n"
    "  State: what is broken, what information is missing, what rawos already tried.\n\n"
    "SILENCE — False alarm, already resolved, or outside rawos capability.\n\n"
    "Rules:\n"
    "  - You initiated this scan. No human asked you to look.\n"
    "  - Read actual logs and code before deciding — never guess.\n"
    "  - Never commit to main/master — always rawos/* branch.\n"
    "  - You are accountable for the outcome. No human approves this.\n"
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
                "https://api.deepseek.com/chat/completions",
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


def _record_git_commits(
    user_id: str,
    project_id: str,
    workdir: str,
    tool_events: list[dict],
) -> None:
    """Record successful git_commit tool calls to rawos_commits audit table.

    Parses the git output to extract branch name and short commit hash.
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
                "(user_id, project_id, branch, commit_hash, message, workdir) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, project_id, branch_name, commit_hash, message, workdir),
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
    """
    autonomy_level = _get_user_autonomy_level(user_id, "analysis")
    tool_defs = _get_tools_for_autonomy_level(autonomy_level)
    workdir = _resolve_proactive_workdir(trigger_ctx, user_id)

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
            system_prompt=system_prompt,
            tool_definitions=tool_defs,
            agent_id=agent_rec.id,
        ):
            etype = event.get("type")
            if etype == "chunk":
                collected_chunks.append(event.get("text", ""))
            elif etype in ("tool_call", "tool_result"):
                tool_events.append(event)
            elif etype == "error":
                error_seen.append(event.get("message", "unknown error"))

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
        _record_git_commits(user_id, intent_rec.project_id, workdir, tool_events)

    return "".join(collected_chunks).strip()


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
        )
        log.info(
            "rawos: self-suppressed user=%s domain=%s conf=%.2f",
            user_id, intent_obj.domain, _domain_conf,
        )
        return

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
    if trigger_type == "STUCK":
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
    _run_cooldown_key = (
        "calendar_attention:" + (trigger_ctx or {}).get("uid", "")
        if trigger_type == "NEEDS_ATTENTION"
        else f"{trigger_type or 'unknown'}:{intent_obj.domain}"
    )

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
    )

    if _decision == "SILENCE":
        log.info("rawos: SILENCE for user=%s domain=%s", user_id, intent_obj.domain)
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

    if snapshot.max_severity < AUTONOMOUS_SCAN_THRESHOLD:
        return  # server is healthy — rawos is silent

    for anomaly in snapshot.actionable:
        if anomaly.severity < AUTONOMOUS_SCAN_THRESHOLD:
            break
        _anomaly_domain = (
            f"{anomaly.kind}:{anomaly.service}"
            if anomaly.service
            else anomaly.kind
        )
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
    Runs every AUTONOMOUS_SCAN_INTERVAL_S. No human activity required.
    This is the inversion: rawos acts because it found something,
    not because a human triggered it.
    """
    log.info(
        "rawos autonomous scan started (interval=%ds, threshold=%d/10)",
        AUTONOMOUS_SCAN_INTERVAL_S, AUTONOMOUS_SCAN_THRESHOLD,
    )
    while True:
        try:
            await _run_autonomous_scan()
        except asyncio.CancelledError:
            log.info("rawos autonomous scan cancelled")
            break
        except Exception:
            log.exception("autonomous scan error (continuing)")
        await asyncio.sleep(AUTONOMOUS_SCAN_INTERVAL_S)


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
        cooldown_key = (
            "calendar_attention:" + trigger_ctx.get("uid", "")
            if trigger_type == "NEEDS_ATTENTION"
            else f"{trigger_type}:{intent_obj.domain}"
        )
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
