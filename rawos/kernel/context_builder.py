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

    return messages, system_addition
