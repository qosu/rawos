"""rawos unified LLM client — single OpenAI-compatible provider.

All LLM access in rawos goes through this module. Configuration comes
from `settings.llm_api_key` / `llm_base_url` / `llm_timeout_s`.
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx

from rawos.config import settings

log = logging.getLogger(__name__)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }


async def complete(
    messages: list[dict],
    *,
    model: str,
    max_tokens: int,
    temperature: float = 0.3,
) -> tuple[str, dict]:
    """Non-streaming completion. Returns (content, usage)."""
    if not settings.llm_api_key:
        raise RuntimeError("LLM_API_KEY not configured")

    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=settings.llm_timeout_s) as client:
        resp = await client.post(
            f"{settings.llm_base_url}/chat/completions",
            json=payload,
            headers=_headers(),
        )
        if resp.status_code != 200:
            raise RuntimeError(f"LLM API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()

    return data["choices"][0]["message"]["content"], data.get("usage", {})


async def tool_call(
    messages: list[dict],
    *,
    tools: list[dict],
    model: str,
    system_prompt: str | None = None,
    max_tokens: int = 4096,
) -> tuple[dict, dict]:
    """Non-streaming tool-call completion. Returns (message, usage)."""
    if not settings.llm_api_key:
        raise RuntimeError("LLM_API_KEY not configured")

    full_messages = messages
    if system_prompt is not None:
        full_messages = [{"role": "system", "content": system_prompt}] + messages

    payload = {
        "model": model,
        "messages": full_messages,
        "tools": tools,
        "tool_choice": "auto",
        "stream": False,
        "max_tokens": max_tokens,
    }
    async with httpx.AsyncClient(timeout=settings.llm_timeout_s) as client:
        resp = await client.post(
            f"{settings.llm_base_url}/chat/completions",
            json=payload,
            headers=_headers(),
        )
        if resp.status_code != 200:
            raise RuntimeError(f"LLM API error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()

    return data["choices"][0]["message"], data.get("usage", {})


async def stream_final(
    messages: list[dict],
    *,
    model: str,
    system_prompt: str | None = None,
    max_tokens: int = 4096,
) -> AsyncIterator[str]:
    """Streaming completion. Yields text deltas."""
    if not settings.llm_api_key:
        raise RuntimeError("LLM_API_KEY not configured")

    full_messages = messages
    if system_prompt is not None:
        full_messages = [{"role": "system", "content": system_prompt}] + messages

    payload = {
        "model": model,
        "messages": full_messages,
        "stream": True,
        "max_tokens": max_tokens,
    }
    async with httpx.AsyncClient(timeout=settings.llm_timeout_s) as client:
        async with client.stream(
            "POST",
            f"{settings.llm_base_url}/chat/completions",
            json=payload,
            headers=_headers(),
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise RuntimeError(f"LLM stream error {resp.status_code}: {body[:200]}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    return
                try:
                    chunk = json.loads(data)
                    text = chunk["choices"][0]["delta"].get("content") or ""
                    if text:
                        yield text
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
