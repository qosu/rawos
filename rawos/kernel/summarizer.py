"""
rawos Summarizer — compress old episodic memories into compact semantic summaries.
Internal use only — never user-facing.
"""
from __future__ import annotations

import json
import logging

from rawos.config import settings
from rawos.kernel import llm_client
from rawos.models import Memory

log = logging.getLogger("rawos.summarizer")

_SUMMARY_PROMPT = """\
Below is a conversation excerpt from a software project workspace.
Summarise it into a compact, factual paragraph that captures:
- What was built or discussed
- Key decisions and outcomes
- Files created (with names and purpose)
- Any important context for future sessions

Output ONLY the summary paragraph, no headings, no markdown."""


async def summarize_memories(memories: list[Memory]) -> str:
    """
    Summarise a list of Memory objects into a compact string.
    Returns empty string if memories is empty or all summaries fail.
    """
    if not memories:
        return ""

    # Build conversation text
    lines = []
    for m in memories:
        role = m.role.value
        content = m.content if isinstance(m.content, str) else json.dumps(m.content)
        lines.append(f"{role.upper()}: {content[:800]}")
    conversation = "\n\n".join(lines)

    return await _complete(_SUMMARY_PROMPT, conversation)


async def _complete(system_prompt: str, user_text: str) -> str:
    """
    Shared internal LLM completion. Returns empty string on any failure.
    Internal use only — never user-facing.
    """
    try:
        content, _usage = await llm_client.complete(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_text[:12_000]},
            ],
            model=settings.llm_summarizer_model,
            max_tokens=512,
            temperature=0.3,
        )
        return content or ""
    except Exception as e:
        log.warning("llm completion failed: %s", e)
        return ""
