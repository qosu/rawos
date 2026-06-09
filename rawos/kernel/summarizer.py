"""
rawos Summarizer — compress old episodic memories into compact semantic summaries.
Uses Groq (fast, cheap) if available, falls back to DeepSeek.
Internal use only — never user-facing.
"""
from __future__ import annotations

import json
import logging

from rawos.config import settings
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

    # Try Groq first (fast + cheap for internal tasks)
    if settings.groq_keys:
        result = await _groq_summarize(conversation)
        if result:
            return result

    # Fallback to DeepSeek
    return await _deepseek_summarize(conversation)


async def _groq_summarize(conversation: str) -> str:
    try:
        import groq as _groq
        import asyncio

        key = settings.groq_keys[0]
        client = _groq.Groq(api_key=key)

        # Groq client is sync — run in executor to not block event loop
        loop = asyncio.get_event_loop()

        def _call():
            resp = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": _SUMMARY_PROMPT},
                    {"role": "user",   "content": conversation[:12_000]},
                ],
                max_tokens=512,
                temperature=0.3,
            )
            return resp.choices[0].message.content or ""

        return await loop.run_in_executor(None, _call)

    except Exception as e:
        log.warning("groq summarization failed: %s", e)
        return ""


async def _deepseek_summarize(conversation: str) -> str:
    try:
        import httpx

        payload = {
            "model": settings.deepseek_model_fast,
            "messages": [
                {"role": "system", "content": _SUMMARY_PROMPT},
                {"role": "user",   "content": conversation[:12_000]},
            ],
            "stream": False,
            "max_tokens": 512,
            "temperature": 0.3,
        }
        headers = {
            "Authorization": f"Bearer {settings.deepseek_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{settings.deepseek_base_url}/chat/completions",
                json=payload, headers=headers,
            )
            if resp.status_code != 200:
                return ""
            data = resp.json()
            return data["choices"][0]["message"]["content"] or ""

    except Exception as e:
        log.warning("deepseek summarization failed: %s", e)
        return ""
