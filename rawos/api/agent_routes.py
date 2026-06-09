"""rawos Agent routes — Phase 4. Read-only; agents are created by the orchestrator."""
from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Any
import rawos.db as db
from rawos.api.deps import current_user
from rawos.models import User

router = APIRouter()


class AgentResponse(BaseModel):
    id:         str
    user_id:    str
    project_id: str
    parent_id:  str | None
    status:     str
    goal:       str
    model:      str
    token_used: int
    created_at: int
    updated_at: int
    children:   list[Any] = []


@router.get("/projects/{project_id}/agents")
async def list_agents(
    project_id: str,
    user: User = Depends(current_user),
) -> list[AgentResponse]:
    agents = db.get_project_agents(user.id, project_id)
    if not agents:
        return []
    agent_map = {a.id: AgentResponse(**a.model_dump()) for a in agents}
    roots = []
    for ar in agent_map.values():
        if ar.parent_id and ar.parent_id in agent_map:
            agent_map[ar.parent_id].children.append(ar)
        else:
            roots.append(ar)
    return roots


@router.get("/projects/{project_id}/agents/{agent_id}")
async def get_agent_detail(
    project_id: str,
    agent_id: str,
    user: User = Depends(current_user),
) -> AgentResponse:
    agent = db.get_agent(user.id, agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="agent not found")
    return AgentResponse(**agent.model_dump())
