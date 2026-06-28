"""
Intent route — core of anima.
Phase 3: context enriched with semantic memory retrieval.
POST /intent → SSE stream of agent events.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import anima.db as db
from anima import billing
from anima import monitoring
from anima.api.deps import current_user
from anima.api.run_registry import Run, registry
from anima.config import settings
from anima.kernel import orchestrator, context_builder, memory_index
from anima.kernel.summarizer import summarize_memories
from anima.models import (
    Agent, AgentStatus, Artifact, ArtifactType,
    Event, EventType, Intent, IntentStatus,
    Memory, MemoryTier, MessageRole, User,
)

log = logging.getLogger("anima.intent")
router = APIRouter()


class IntentRequest(BaseModel):
    project_id: str
    message:    str
    model:      str | None = None


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

async def _index_memories_bg(user_id: str, project_id: str, memory_ids: list[str]) -> None:
    """Index newly saved memories into ChromaDB (runs after SSE stream completes)."""
    for mid in memory_ids:
        m = db.get_memory_by_id(user_id, mid)
        if m is None:
            continue
        content = m.content if isinstance(m.content, str) else json.dumps(m.content)
        memory_index.upsert_memory(
            memory_id=m.id,
            text=content,
            project_id=project_id,
            user_id=user_id,
            tier=m.tier.value,
            role=m.role.value,
            created_at=m.created_at,
        )


async def _index_files_bg(user_id: str, project_id: str) -> None:
    """Re-index any project files that are new or changed."""
    artifacts = db.get_project_artifacts(user_id, project_id)
    for artifact in artifacts:
        if not artifact.path:
            continue
        p = Path(artifact.path)
        if not p.exists() or not p.is_file():
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
            if content.strip():
                memory_index.upsert_file(
                    file_id=artifact.id,
                    content=content,
                    project_id=project_id,
                    user_id=user_id,
                    file_path=artifact.name,
                    file_name=artifact.name,
                )
        except OSError:
            pass


async def _maybe_summarize_bg(user_id: str, project_id: str) -> None:
    """
    If episodic memory count exceeds threshold, summarise the oldest N memories
    and replace them with a single semantic summary.
    """
    count = db.get_memory_count(user_id, project_id)
    if count <= settings.summarize_after_n_memories:
        return

    oldest = db.get_episodic_oldest(user_id, project_id, settings.summarize_oldest_n)
    if len(oldest) < 10:   # not enough to bother summarising
        return

    log.info("summarising %d memories for project %s", len(oldest), project_id)
    summary_text = await summarize_memories(oldest)
    if not summary_text.strip():
        log.warning("summarisation returned empty text for project %s", project_id)
        return

    # Save summary as semantic memory
    summary_mem = Memory(
        user_id=user_id,
        project_id=project_id,
        tier=MemoryTier.SEMANTIC,
        role=MessageRole.SYSTEM,
        content=summary_text,
    )
    db.save_memory(summary_mem)
    memory_index.upsert_memory(
        memory_id=summary_mem.id,
        text=summary_text,
        project_id=project_id,
        user_id=user_id,
        tier="semantic",
        role="system",
        created_at=summary_mem.created_at,
    )

    # Delete the original episodic memories
    ids_to_delete = [m.id for m in oldest]
    db.delete_memories_batch(user_id, ids_to_delete)
    memory_index.delete_memories_batch(ids_to_delete)

    log.info(
        "summarised %d→1 memories for project %s (saved %d chars)",
        len(oldest), project_id, len(summary_text),
    )


# ---------------------------------------------------------------------------
# Orchestration lifecycle (decoupled from the SSE connection — Stage F)
# ---------------------------------------------------------------------------

async def _run_orchestration(
    run: Run,
    intent: Intent,
    user: User,
    project_id: str,
    raw_message: str,
    model: str | None,
) -> None:
    """Run one full agent turn to completion, independent of any subscriber.

    Always finalises (memory, intent status, billing, background indexing)
    in a `finally`, regardless of whether a client is still connected to the
    SSE stream. Events are published via `registry.append` so both the
    original POST stream and any later `GET /intent/{run_id}/stream`
    reconnect can observe them.
    """
    _intent_start = time.perf_counter()
    monitoring.active_sse_connections.inc()

    chosen_model = model or settings.llm_agent_model
    error_occurred = False
    response_chunks: list[str] = []
    user_mem_id: str | None = None
    asst_mem_id: str | None = None

    await registry.append(run, {"type": "run_started", "run_id": run.run_id})

    try:
        project = db.get_project(user.id, project_id)
        if not project:
            error_occurred = True
            await registry.append(run, {"type": "error", "message": "project not found"})
            return
        if not project.workdir:
            error_occurred = True
            await registry.append(run, {"type": "error", "message": "project workdir not initialised"})
            return

        # Create intent + agent records
        db.create_intent(intent)

        agent = Agent(user_id=user.id, project_id=project_id, goal=raw_message[:200],
                      model=chosen_model)
        agent = agent.transition(AgentStatus.ACTIVE)
        db.create_agent(agent)
        db.update_intent(user.id, intent.id, agent_id=agent.id, status=IntentStatus.EXECUTING)
        db.log_event(Event(user_id=user.id, project_id=project_id, agent_id=agent.id,
                           type=EventType.AGENT_STARTED, payload={"intent_id": intent.id}))

        # Save user message to episodic memory
        user_mem = Memory(user_id=user.id, project_id=project_id, agent_id=agent.id,
                          tier=MemoryTier.EPISODIC, role=MessageRole.USER, content=raw_message)
        db.save_memory(user_mem)
        user_mem_id = user_mem.id

        # Build enriched context (recent history + semantic retrieval)
        messages, system_ctx = context_builder.build_context(user.id, project_id, raw_message)
        # Remove the just-saved user message from history to avoid duplication
        if messages and messages[-1]["role"] == "user" and messages[-1]["content"] == raw_message:
            messages = messages[:-1]
        messages.append({"role": "user", "content": raw_message})

        # Keep system message static (cache-prefix anchor) - merge dynamic
        # per-turn context into the final user message instead.
        context_builder.merge_dynamic_context(messages, system_ctx)
        from anima.kernel.agent_loop import _SYSTEM_PROMPT as BASE_PROMPT
        enriched_system = BASE_PROMPT

        async def on_artifact(af_meta: dict) -> Artifact:
            art = Artifact(
                user_id=user.id, project_id=project_id, agent_id=agent.id,
                intent_id=intent.id,
                type=ArtifactType.FILE,
                name=af_meta["name"],
                path=af_meta["path"],
                mime_type=af_meta["mime_type"],
                size_bytes=af_meta["size_bytes"],
            )
            db.save_artifact(art)
            return art

        try:
            async for event in orchestrator.run(
                user_id=user.id,
                project_id=project_id,
                intent_id=intent.id,
                messages=messages,
                workdir=project.workdir,
                model=chosen_model,
                on_artifact=on_artifact,
                system_prompt=enriched_system,
            ):
                if event["type"] == "chunk":
                    response_chunks.append(event["text"])
                elif event["type"] == "error":
                    error_occurred = True

                db.log_event(Event(
                    user_id=user.id, project_id=project_id, agent_id=agent.id,
                    type=EventType.TOOL_CALLED if event["type"] == "tool_call" else EventType.TASK_COMPLETED,
                    payload=event,
                ))
                await registry.append(run, event)

        except Exception as e:
            log.exception("agent loop uncaught: %s", e)
            error_occurred = True
            await registry.append(run, {"type": "error", "message": str(e)})

        # Persist assistant response
        if response_chunks:
            asst_mem = Memory(
                user_id=user.id, project_id=project_id, agent_id=agent.id,
                tier=MemoryTier.EPISODIC, role=MessageRole.ASSISTANT,
                content="".join(response_chunks),
            )
            db.save_memory(asst_mem)
            asst_mem_id = asst_mem.id

        # Finalise records
        final_status = IntentStatus.FAILED if error_occurred else IntentStatus.COMPLETED
        db.update_intent(user.id, intent.id, status=final_status)
        db.update_agent_status(user.id, agent.id, AgentStatus.ARCHIVED)
        db.log_event(Event(
            user_id=user.id, project_id=project_id, agent_id=agent.id,
            type=EventType.ERROR if error_occurred else EventType.TASK_COMPLETED,
            payload={"intent_id": intent.id, "chars": len("".join(response_chunks))},
        ))

        # Record token usage for billing and metrics
        if response_chunks:
            # Approximate: 1 token ≈ 4 chars (rough but consistent)
            approx_tokens = max(1, len("".join(response_chunks)) // 4)
            billing.record_usage(user.id, approx_tokens, model=chosen_model, intent_id=intent.id)
            monitoring.intent_tokens_total.labels(
                model=chosen_model, user_tier=user.tier.value
            ).inc(approx_tokens)

        # Background: index new memories + files into ChromaDB, maybe summarise
        new_ids = ([user_mem_id] if user_mem_id else []) + ([asst_mem_id] if asst_mem_id else [])
        asyncio.create_task(_index_memories_bg(user.id, project_id, new_ids))
        asyncio.create_task(_index_files_bg(user.id, project_id))
        asyncio.create_task(_maybe_summarize_bg(user.id, project_id))

    finally:
        status = "failed" if error_occurred else "completed"
        await registry.finish(run, status)
        monitoring.active_sse_connections.dec()
        monitoring.intent_duration_seconds.labels(model=model or "default").observe(
            time.perf_counter() - _intent_start
        )


@router.post("")
async def create_intent(body: IntentRequest, user: User = Depends(current_user)):
    try:
        intent = Intent(user_id=user.id, project_id=body.project_id, raw_text=body.message,
                        status=IntentStatus.ROUTING)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        billing.check_quota(user.id, user.tier.value)
    except billing.QuotaExceeded as e:
        raise HTTPException(status_code=429, detail=str(e))

    run = registry.create(intent.id, user.id)
    asyncio.create_task(_run_orchestration(run, intent, user, body.project_id, body.message, body.model))

    return StreamingResponse(
        registry.subscribe(run, after_seq=0),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{run_id}/stream")
async def stream_intent(run_id: str, request: Request, user: User = Depends(current_user)):
    after_seq = 0
    last_event_id = request.headers.get("last-event-id") or request.query_params.get("after")
    if last_event_id is not None:
        try:
            after_seq = int(last_event_id)
        except ValueError:
            after_seq = 0

    run = registry.get(run_id)
    if run is None or run.user_id != user.id:
        raise HTTPException(status_code=404, detail="run not found or expired")

    return StreamingResponse(
        registry.subscribe(run, after_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
