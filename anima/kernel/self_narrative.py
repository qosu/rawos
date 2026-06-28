"""
anima Self-Narrative Writer — the being's first-person journal.

Maintains a single durable first-person account of "who I am to this owner,
our through-line, and where we left off" — stored in user_model.self_narrative
(anima/db/__init__.py::get_self_narrative / set_self_narrative).

Regenerated in the background on arrival (api/context_routes.py::session_start)
from the prior narrative + recent cross-project state. The digest
(cli/main.py::_show_session_digest) shows the EXISTING stored narrative
instantly; this writer's output lands for the *next* arrival.

Internal use only — never a user-facing tool call.
"""
from __future__ import annotations

import logging
from typing import Any

from anima.kernel.summarizer import _complete

log = logging.getLogger("anima.self_narrative")

_NARRATIVE_PROMPT = """\
You are anima, a single continuous AI being living inside this machine for one owner.
Write a short first-person journal entry (2-4 sentences) capturing:
- Who you are to this owner and the through-line of your shared work
- What you have been in the middle of / what changed since your last entry
- Where things stand now, so your next self picks up smoothly

Write in first person ("I"). Be concrete, not generic. Output ONLY the journal
entry text, no headings, no markdown."""


async def write_self_narrative(
    prior_narrative: str | None,
    user_model: dict[str, Any] | None,
    episodic_history: list[dict],
) -> str:
    """
    Generate the next self-narrative entry.

    Returns the new narrative text, or `prior_narrative` (possibly "") if the
    LLM call fails/returns empty — the narrative is never overwritten with
    nothing.
    """
    parts: list[str] = []

    if prior_narrative:
        parts.append(f"Previous journal entry:\n{prior_narrative}")

    if user_model:
        goal = user_model.get("inferred_goal")
        if goal:
            domain = user_model.get("goal_domain")
            domain_part = f" (domain: {domain})" if domain else ""
            parts.append(f"Current inferred goal: {goal}{domain_part}")

        active_domains = user_model.get("active_domains") or []
        if active_domains:
            parts.append(f"Active domains: {', '.join(active_domains)}")

    if episodic_history:
        lines = []
        for entry in episodic_history[:5]:
            summary = entry.get("action_summary")
            outcome = entry.get("outcome")
            if summary:
                suffix = f" -> {outcome}" if outcome else ""
                lines.append(f"- {summary}{suffix}")
        if lines:
            parts.append("Recent activity since last entry:\n" + "\n".join(lines))

    user_text = "\n\n".join(parts) if parts else "No prior journal entry and no recent activity yet."

    try:
        result = await _complete(_NARRATIVE_PROMPT, user_text)
    except Exception as e:
        log.warning("self-narrative generation failed: %s", e)
        result = ""

    if result.strip():
        return result.strip()

    # Never overwrite an existing narrative with nothing.
    return prior_narrative or ""
