"""
rawos Agent Loop — agentic tool-use execution engine.

Protocol:
  Rounds 1..MAX_TOOL_ROUNDS: non-streaming LLM call with tools
    - If tool_calls: execute all, append to messages, loop
    - If no tool_calls: we have the final answer — stream it
  If MAX_TOOL_ROUNDS exhausted: stream a final summarising call (tool_choice=none)

SSE event types yielded (as dicts, caller serialises to JSON):
  {"type": "tool_call",   "call_id": str, "tool": str, "input": dict}
  {"type": "tool_result", "call_id": str, "tool": str, "output": str, "success": bool, "duration_ms": int}
  {"type": "chunk",       "text": str}
  {"type": "artifact",    "id": str, "name": str, "mime_type": str, "size_bytes": int, "path": str}
  {"type": "done",        "intent_id": str}
  {"type": "error",       "message": str}
"""
from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from rawos.config import settings
from rawos.kernel.tools import TOOL_DEFINITIONS, execute as execute_tool

log = logging.getLogger("rawos.agent_loop")

MAX_TOOL_ROUNDS = 12

_SYSTEM_PROMPT = """You are rawos — an AI operating system with real execution capabilities.

You have tools to create files, run commands, and build real software in the user's workspace.

RULES:
- When asked to build or create anything: DO IT using the tools. Never just describe it.
- Always use write_file to create actual files.
- After writing files, use bash to verify they exist correctly.
- Use list_files to understand the workspace state before starting.
- Be direct and effective — show results, not plans.
- For websites: create complete, self-contained HTML/CSS/JS files.

The workspace is isolated to the user's project directory.
Be concise in your final response — the work speaks for itself."""


async def _llm_tool_call(
    messages: list[dict],
    model: str,
    system_prompt: str | None = None,
    tool_definitions: list[dict] | None = None,
) -> dict:
    """Non-streaming LLM call for tool selection. Returns the full message dict."""
    if not settings.deepseek_key:
        raise RuntimeError("DEEPSEEK_KEY not configured")

    active_tools = tool_definitions if tool_definitions is not None else TOOL_DEFINITIONS
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt or _SYSTEM_PROMPT}] + messages,
        "tools": active_tools,
        "tool_choice": "auto",
        "stream": False,
        "max_tokens": 4096,
    }
    headers = {
        "Authorization": f"Bearer {settings.deepseek_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{settings.deepseek_base_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        if resp.status_code != 200:
            body = resp.text[:300]
            raise RuntimeError(f"DeepSeek {resp.status_code}: {body}")
        data = resp.json()
    return data["choices"][0]["message"]


async def _llm_stream_final(
    messages: list[dict],
    model: str,
    system_prompt: str | None = None,
) -> AsyncIterator[str]:
    """Streaming LLM call for the final answer (no tools)."""
    if not settings.deepseek_key:
        raise RuntimeError("DEEPSEEK_KEY not configured")

    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system_prompt or _SYSTEM_PROMPT}] + messages,
        "stream": True,
        "max_tokens": 4096,
    }
    headers = {
        "Authorization": f"Bearer {settings.deepseek_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST",
            f"{settings.deepseek_base_url}/chat/completions",
            json=payload,
            headers=headers,
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise RuntimeError(f"DeepSeek stream {resp.status_code}: {body[:200]}")
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


def _detect_artifacts(workdir: str, known_files: set[str]) -> list[dict]:
    """
    Scan workdir for new files not in known_files.
    Returns artifact metadata dicts for each new file.
    """
    workdir_path = Path(workdir).resolve()
    artifacts = []
    try:
        for fpath in workdir_path.rglob("*"):
            if not fpath.is_file():
                continue
            rel = str(fpath.relative_to(workdir_path))
            if rel in known_files:
                continue
            known_files.add(rel)
            mime, _ = mimetypes.guess_type(str(fpath))
            size = fpath.stat().st_size
            artifacts.append({
                "name":      rel,
                "path":      str(fpath),
                "mime_type": mime or "application/octet-stream",
                "size_bytes": size,
            })
    except OSError:
        pass
    return artifacts


async def run(
    messages:        list[dict],
    workdir:         str,
    model:           str,
    intent_id:       str,
    on_artifact:     Any = None,
    system_prompt:   str | None = None,
    tool_definitions: list[dict] | None = None,
    agent_id:        str = "",
) -> AsyncIterator[dict]:
    """
    Run the agentic loop. Yields SSE event dicts.
    messages: history WITHOUT system prompt (caller provides).
    on_artifact: async callable invoked for each new file, returns Artifact with id.
    """
    # Snapshot existing files so we detect new ones after tool calls
    existing: set[str] = set()
    _detect_artifacts(workdir, existing)   # pre-populate, discard result

    working_messages = list(messages)

    for round_num in range(MAX_TOOL_ROUNDS):
        try:
            msg = await _llm_tool_call(working_messages, model, system_prompt, tool_definitions)
        except Exception as e:
            log.error("llm_tool_call failed: %s", e)
            yield {"type": "error", "message": str(e)}
            return

        tool_calls = msg.get("tool_calls") or []
        text_content = (msg.get("content") or "").strip()

        if not tool_calls:
            # No tool calls: this IS the final answer.
            # If content is non-empty, stream it chunk by chunk (already have it).
            if text_content:
                # Emit as a single chunk (already received, no streaming benefit here
                # unless we make another streaming call — but that wastes tokens).
                # Emit char-by-char to maintain streaming UX.
                chunk_size = 4
                for i in range(0, len(text_content), chunk_size):
                    yield {"type": "chunk", "text": text_content[i:i+chunk_size]}
            else:
                # Empty content after tools: make one final streaming call
                try:
                    async for chunk in _llm_stream_final(working_messages, model, system_prompt):
                        yield {"type": "chunk", "text": chunk}
                except Exception as e:
                    yield {"type": "error", "message": str(e)}
            break

        # Execute tool calls
        # Append assistant message with tool_calls to history
        working_messages.append({
            "role": "assistant",
            "content": text_content or None,
            "tool_calls": tool_calls,
        })

        for tc in tool_calls:
            call_id   = tc.get("id", "")
            func      = tc.get("function", {})
            tool_name = func.get("name", "")
            try:
                params = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                params = {}

            yield {"type": "tool_call", "call_id": call_id, "tool": tool_name, "input": params}

            result = await execute_tool(tool_name, params, workdir)

            yield {
                "type":        "tool_result",
                "call_id":     call_id,
                "tool":        tool_name,
                "output":      result.output,
                "success":     result.success,
                "duration_ms": result.duration_ms,
            }

            # Append tool result to history
            working_messages.append({
                "role":         "tool",
                "tool_call_id": call_id,
                "content":      result.output,
            })

            # Detect and emit new files created by this tool call
            new_files = _detect_artifacts(workdir, existing)
            for af in new_files:
                artifact_id = ""
                if on_artifact:
                    try:
                        art = await on_artifact(af)
                        artifact_id = art.id
                    except Exception:
                        log.exception("on_artifact callback failed")
                yield {
                    "type":       "artifact",
                    "id":         artifact_id,
                    "name":       af["name"],
                    "mime_type":  af["mime_type"],
                    "size_bytes": af["size_bytes"],
                    "path":       af["path"],
                }

    else:
        # Max rounds reached — do a final streaming summarisation
        log.warning("agent loop hit MAX_TOOL_ROUNDS=%d for intent %s", MAX_TOOL_ROUNDS, intent_id)
        try:
            async for chunk in _llm_stream_final(working_messages, model, system_prompt):
                yield {"type": "chunk", "text": chunk}
        except Exception as e:
            yield {"type": "error", "message": str(e)}

    yield {"type": "done", "intent_id": intent_id, "agent_id": agent_id}
