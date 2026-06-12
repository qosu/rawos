"""
rawos Context Builder — enrich intent context with semantic memory retrieval.

Build strategy per intent:
  1. Recent episodic memories (last 20, chronological — exact recency)
  2. Semantic search (top-5 relevant past memories — long-term recall)
  3. File context (top-3 relevant file contents — project artefacts)
  4. Deduplication (skip semantic results already in recent window)
  5. Inject as system prompt addition

The result is a (messages_list, system_context_str) tuple.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import rawos.db as db
from rawos.config import settings
from rawos.context.user_model import get_user_model
from rawos.kernel import memory_index

log = logging.getLogger("rawos.context_builder")

_RECENT_N = 20


def _content_str(memory_content) -> str:
    if isinstance(memory_content, str):
        return memory_content
    return json.dumps(memory_content, ensure_ascii=False)


def build_context(
    user_id:    str,
    project_id: str,
    query:      str,
) -> tuple[list[dict], str]:
    """
    Synchronous context builder (ChromaDB ops are CPU-bound, not async).
    Returns (messages, system_context_addition).

    messages: recent episodic history as OpenAI-format message dicts
    system_context_addition: semantic context to append to system prompt
    """
    # 1. Recent episodic memories (last N, chronological)
    recent = db.get_project_memories(
        user_id, project_id, tier="episodic", limit=_RECENT_N
    )
    messages: list[dict] = []
    recent_content_set: set[str] = set()

    for m in recent:
        role = m.role.value
        if role not in ("user", "assistant"):
            continue
        text = _content_str(m.content)
        messages.append({"role": role, "content": text})
        recent_content_set.add(text[:200])   # fingerprint for dedup

    # 2 + 3. Semantic search (best-effort — empty result if index not ready)
    semantic_parts: list[str] = []

    try:
        mem_results = memory_index.search_memories(
            project_id, query, n_results=settings.semantic_context_results
        )
        for doc, meta in mem_results:
            snippet = doc[:400]
            if snippet[:200] in recent_content_set:
                continue   # already in recent window
            role = meta.get("role", "")
            semantic_parts.append(f"[{role}] {snippet}")

        file_results = memory_index.search_files(
            project_id, query, n_results=settings.file_context_results
        )
        for doc, meta in file_results:
            path = meta.get("file_path", "?")
            semantic_parts.append(f"[file: {path}]\n{doc[:600]}")

    except Exception as e:
        log.debug("semantic context retrieval skipped: %s", e)

    system_addition = ""
    if semantic_parts:
        body = "\n\n".join(semantic_parts)
        system_addition = (
            "\n\n<project_memory>\n"
            "Relevant context from this project's history and files:\n\n"
            + body
            + "\n</project_memory>"
        )

    continuity_block = _build_continuity_block(user_id)
    if continuity_block:
        system_addition = continuity_block + system_addition

    return messages, system_addition


def _build_continuity_block(user_id: str) -> str:
    """
    Cross-project continuity: the being's one continuous life, regardless of
    which project the current turn is scoped to.

    Best-effort — any failure or absent user_model yields "" so callers
    (and existing tests asserting system_addition == "") are unaffected.
    """
    try:
        model = get_user_model(user_id)
        if not model:
            return ""

        lines: list[str] = []

        goal = model.get("inferred_goal")
        if goal:
            confidence = model.get("goal_confidence") or 0.0
            domain = model.get("goal_domain")
            domain_part = f" (domain: {domain})" if domain else ""
            lines.append(f"Current goal: {goal}{domain_part} [confidence: {confidence:.0%}]")

        active_domains = model.get("active_domains") or []
        if active_domains:
            lines.append(f"Active domains: {', '.join(active_domains)}")

        recent_activity = model.get("recent_activity") or []
        if recent_activity:
            previews = []
            for entry in recent_activity[:5]:
                preview = entry.get("preview") or entry.get("file") or entry.get("name")
                if preview:
                    previews.append(f"- {preview}")
            if previews:
                lines.append("Recent activity:\n" + "\n".join(previews))

        episodic_history = model.get("episodic_history") or []
        if episodic_history:
            actions = []
            for entry in episodic_history[:3]:
                summary = entry.get("action_summary")
                outcome = entry.get("outcome")
                if summary:
                    suffix = f" -> {outcome}" if outcome else ""
                    actions.append(f"- {summary}{suffix}")
            if actions:
                lines.append("Recent proactive work:\n" + "\n".join(actions))

        if not lines:
            return ""

        return (
            "\n\n<continuity>\n"
            "Cross-project context — the through-line of this being's life:\n\n"
            + "\n".join(lines)
            + "\n</continuity>"
        )
    except Exception as e:
        log.debug("continuity context retrieval skipped: %s", e)
        return ""
