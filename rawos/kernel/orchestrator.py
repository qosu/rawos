"""
rawos Orchestrator — Phase 4 multi-agent coordination engine.

Protocol:
  1. CLASSIFY: non-streaming DeepSeek call → {"mode": "direct"} | {"mode": "multi", "tasks": [...]}
  2. If "direct": delegate to agent_loop.run() unchanged.
  3. If "multi":
     a. Emit orchestrator_plan event.
     b. Create Agent DB records (parent_id = orchestrator.id).
     c. Emit agent_spawn events.
     d. Execute tasks respecting dependency graph (parallel where possible).
     e. Merge event streams via asyncio.Queue.
     f. Emit final synthesis stream.

SSE event types added in Phase 4:
  {"type": "orchestrator_plan", "plan": [{"id", "goal", "agent_type", "depends_on"}]}
  {"type": "agent_spawn", "agent_id", "agent_type", "goal", "parent_id"}
  {"type": "agent_status", "agent_id", "status"}  — "running" | "done" | "failed"
  {"type": "agent_output", "agent_id", "content"}  — streaming output from sub-agent
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

from rawos.config import settings
from rawos.models import Agent, AgentStatus, Event, EventType, MemoryTier, MessageRole, Memory
import rawos.db as db
from rawos.kernel import agent_loop, llm_client
from rawos.kernel.specialized_agents import get_tool_definitions, get_system_prompt

log = logging.getLogger("rawos.orchestrator")

_DONE_SENTINEL = "__agent_done__"
_AGENT_TIMEOUT    = 300.0  # seconds max per sub-agent

_DECOMPOSE_PROMPT = """\
You are the rawos Orchestrator. Given a user intent, decide whether it should be handled by a \
single agent or broken into parallel specialised sub-tasks.

Reply ONLY with valid JSON — no explanation, no markdown.

If the task is conversational, analytical, a simple question, or can be handled by one agent:
{"mode": "direct"}

If the task genuinely benefits from parallel specialised agents (e.g. build + design + research happening concurrently):
{"mode": "multi", "tasks": [
  {"id": "t1", "agent_type": "code|design|research|data", "goal": "<precise self-contained instruction>", "depends_on": []},
  {"id": "t2", "agent_type": "...", "goal": "...", "depends_on": ["t1"]}
]}

Rules:
- Maximum 5 tasks total.
- agent_type must be one of: code, design, research, data.
- Each task goal is a complete, self-contained instruction — sub-agents have no other context.
- depends_on lists task ids whose output this task requires before starting.
- Use "direct" for anything conversational, analytical, short, or single-agent-capable.
"""

_SYNTHESIS_PROMPT = """\
You are rawos. Multiple specialised agents have completed their work.
Synthesise their outputs into a coherent, concise response for the user.
Focus on results: what was built, what was found, what the user can do next.
Be concise — the work speaks for itself.
"""


async def _classify_intent(
    messages: list[dict],
    model: str,
) -> dict:
    """
    Ask the configured LLM provider whether to handle directly or spawn sub-agents.
    Returns {"mode": "direct"} or {"mode": "multi", "tasks": [...]}.
    Falls back to {"mode": "direct"} on any error.
    """
    if not settings.llm_api_key:
        return {"mode": "direct"}

    try:
        content, _usage = await llm_client.complete(
            [
                {"role": "system", "content": _DECOMPOSE_PROMPT},
            ] + messages[-6:],   # last 6 messages = sufficient context, cheap
            model=model,
            max_tokens=1024,
            temperature=0.0,
        )
        raw = content.strip()
        plan = json.loads(raw)
        if plan.get("mode") not in ("direct", "multi"):
            return {"mode": "direct"}
        tasks = plan.get("tasks", [])
        if plan["mode"] == "multi" and not (1 <= len(tasks) <= settings.max_parallel_agents):
            return {"mode": "direct"}
        return plan
    except Exception as e:
        log.warning("classify intent error: %s — falling back to direct", e)
        return {"mode": "direct"}


async def _run_sub_agent(
    task: dict,
    agent: Agent,
    completed_outputs: dict[str, str],
    base_messages: list[dict],
    workdir: str,
    model: str,
    intent_id: str,
    on_artifact: Any,
    queue: asyncio.Queue,
) -> None:
    """
    Execute one sub-agent, pushing all events to queue.
    Pushes a final sentinel {"type": _DONE_SENTINEL, "task_id", "agent_id", "output"}.
    """
    agent_type = task["agent_type"]
    task_id = task["id"]

    # Build context: inject completed dependency outputs into messages
    dep_context = ""
    for dep_id in task.get("depends_on", []):
        if dep_id in completed_outputs:
            dep_context += f"\n\n[Output from task {dep_id}]:\n{completed_outputs[dep_id]}"

    task_messages = list(base_messages)
    if dep_context:
        # Inject dependency context as an extra user turn
        task_messages = task_messages + [
            {"role": "user", "content": f"Context from previous tasks:{dep_context}"},
            {"role": "assistant", "content": "Understood. Proceeding with my task."},
        ]
    # Final task instruction
    task_messages = task_messages + [{"role": "user", "content": task["goal"]}]

    system = get_system_prompt(agent_type)
    tool_defs = get_tool_definitions(agent_type)

    output_chunks: list[str] = []
    try:
        async for event in agent_loop.run(
            messages=task_messages,
            workdir=workdir,
            model=model,
            intent_id=intent_id,
            user_id=agent.user_id,
            on_artifact=on_artifact,
            system_prompt=system,
            tool_definitions=tool_defs,
            agent_id=agent.id,
        ):
            # Tag every event with agent_id
            tagged = {**event, "agent_id": agent.id, "agent_type": agent_type}
            if event["type"] == "chunk":
                output_chunks.append(event["text"])
                # Re-emit as agent_output so frontend knows which agent produced it
                await queue.put({
                    "type": "agent_output",
                    "agent_id": agent.id,
                    "content": event["text"],
                })
            else:
                await queue.put(tagged)
        output = "".join(output_chunks)
        db.update_agent_status(agent.user_id, agent.id, AgentStatus.ARCHIVED)
    except Exception as e:
        log.exception("sub-agent %s failed: %s", agent.id, e)
        await queue.put({
            "type": "agent_status",
            "agent_id": agent.id,
            "status": "failed",
            "error": str(e),
        })
        output = f"[FAILED: {e}]"
        db.update_agent_status(agent.user_id, agent.id, AgentStatus.ARCHIVED)

    await queue.put({
        "type": _DONE_SENTINEL,
        "task_id": task_id,
        "agent_id": agent.id,
        "output": output,
    })


async def run(
    user_id: str,
    project_id: str,
    intent_id: str,
    messages: list[dict],
    workdir: str,
    model: str,
    on_artifact: Any = None,
    system_prompt: str | None = None,
) -> AsyncIterator[dict]:
    """
    Top-level orchestration entry point. Replaces direct agent_loop.run() calls.

    Decides single vs multi-agent, then either delegates or orchestrates.
    Yields the same SSE event types as agent_loop.run(), plus Phase 4 types.
    """
    # Step 1: Classify
    plan = await _classify_intent(messages, model)

    if plan["mode"] == "direct":
        # Single-agent path — identical to pre-Phase-4 behaviour
        async for event in agent_loop.run(
            messages=messages,
            workdir=workdir,
            model=model,
            intent_id=intent_id,
            user_id=user_id,
            on_artifact=on_artifact,
            system_prompt=system_prompt,
        ):
            yield event
        return

    # Multi-agent path
    tasks: list[dict] = plan["tasks"]
    log.info("multi-agent: %d tasks for intent %s", len(tasks), intent_id)

    # Emit plan
    yield {"type": "orchestrator_plan", "plan": tasks}

    # Create orchestrator Agent record
    orch_agent = Agent(
        user_id=user_id,
        project_id=project_id,
        goal=f"orchestrate {len(tasks)} tasks",
        model=model,
    )
    orch_agent = orch_agent.transition(AgentStatus.ACTIVE)
    db.create_agent(orch_agent)

    # Create sub-agent DB records and emit spawn events
    sub_agents: dict[str, tuple[Agent, dict]] = {}
    for task in tasks:
        sub = Agent(
            user_id=user_id,
            project_id=project_id,
            parent_id=orch_agent.id,
            goal=task["goal"][:200],
            model=model,
        )
        sub = sub.transition(AgentStatus.ACTIVE)
        db.create_agent(sub)
        sub_agents[task["id"]] = (sub, task)
        yield {
            "type":       "agent_spawn",
            "agent_id":   sub.id,
            "agent_type": task["agent_type"],
            "goal":       task["goal"],
            "parent_id":  orch_agent.id,
        }

    # Execute plan with dependency ordering
    queue: asyncio.Queue = asyncio.Queue()
    completed_outputs: dict[str, str] = {}
    pending: dict[str, dict] = {t["id"]: t for t in tasks}
    running_task_ids: set[str] = set()
    asyncio_tasks: dict[str, asyncio.Task] = {}
    done_count = 0
    total = len(tasks)

    while done_count < total:
        # Dispatch all tasks whose deps are satisfied
        ready = [
            (tid, t)
            for tid, t in list(pending.items())
            if all(dep in completed_outputs for dep in t.get("depends_on", []))
            and tid not in running_task_ids
        ]
        for tid, task in ready:
            sub_agent, _ = sub_agents[tid]
            running_task_ids.add(tid)
            del pending[tid]
            yield {
                "type":     "agent_status",
                "agent_id": sub_agent.id,
                "status":   "running",
            }
            asyncio_tasks[tid] = asyncio.create_task(
                _run_sub_agent(
                    task=task,
                    agent=sub_agent,
                    completed_outputs=dict(completed_outputs),  # snapshot at dispatch time
                    base_messages=messages,
                    workdir=workdir,
                    model=model,
                    intent_id=intent_id,
                    on_artifact=on_artifact,
                    queue=queue,
                )
            )

        # Drain one event, with timeout guard
        try:
            event = await asyncio.wait_for(queue.get(), timeout=_AGENT_TIMEOUT)
        except asyncio.TimeoutError:
            log.error("orchestrator queue timeout after %ss — aborting", _AGENT_TIMEOUT)
            yield {"type": "error", "message": "agent timeout — task took too long"}
            break

        if event["type"] == _DONE_SENTINEL:
            task_id = event["task_id"]
            completed_outputs[task_id] = event["output"]
            done_count += 1
            sub_agent, _ = sub_agents[task_id]
            yield {
                "type":     "agent_status",
                "agent_id": event["agent_id"],
                "status":   "done",
            }
        else:
            yield event

    # Wait for all asyncio tasks to finish cleanly
    if asyncio_tasks:
        await asyncio.gather(*asyncio_tasks.values(), return_exceptions=True)

    # Archive orchestrator agent
    db.update_agent_status(user_id, orch_agent.id, AgentStatus.ARCHIVED)

    # Step 3: Synthesis — combine all outputs into a coherent final response
    synthesis_messages = list(messages)
    summaries = []
    for tid, task in [(t["id"], t) for t in tasks]:
        out = completed_outputs.get(tid, "[no output]")
        summaries.append(f"**{task['agent_type'].title()} Agent** — {task['goal']}\n{out[:2000]}")
    synthesis_context = "\n\n---\n\n".join(summaries)
    synthesis_messages.append({
        "role": "user",
        "content": (
            f"All agents have completed their work. Here are their outputs:\n\n{synthesis_context}"
            f"\n\nPlease synthesise these into a coherent response for the user."
        ),
    })

    try:
        async for event in agent_loop.run(
            messages=synthesis_messages,
            workdir=workdir,
            model=model,
            intent_id=intent_id,
            user_id=user_id,
            on_artifact=on_artifact,
            system_prompt=_SYNTHESIS_PROMPT,
            tool_definitions=[],  # synthesis agent uses no tools
        ):
            yield event
    except Exception as e:
        log.exception("synthesis failed: %s", e)
        yield {"type": "error", "message": f"synthesis failed: {e}"}
