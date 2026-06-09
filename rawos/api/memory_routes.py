"""
Memory management routes — list, delete, and create project memories.
Used by the "Project Memory" UI panel.
"""
from __future__ import annotations

import rawos.db as db
from rawos.api.deps import current_user
from rawos.kernel import memory_index
from rawos.models import Memory, MemoryTier, MessageRole, User
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

router = APIRouter()


class MemoryResponse(BaseModel):
    id:         str
    tier:       str
    role:       str
    content:    str
    created_at: int


class CreateMemoryRequest(BaseModel):
    content: str
    tier:    str = "semantic"
    role:    str = "system"


def _to_response(m: Memory) -> MemoryResponse:
    content = m.content if isinstance(m.content, str) else str(m.content)
    return MemoryResponse(
        id=m.id, tier=m.tier.value, role=m.role.value,
        content=content, created_at=m.created_at,
    )


@router.get("/projects/{project_id}/memories")
async def list_memories(
    project_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(current_user),
) -> list[MemoryResponse]:
    project = db.get_project(user.id, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    memories = db.get_all_project_memories(user.id, project_id, limit=limit)
    return [_to_response(m) for m in memories]


@router.delete("/projects/{project_id}/memories/{memory_id}", status_code=204)
async def delete_memory(
    project_id: str,
    memory_id:  str,
    user: User = Depends(current_user),
) -> None:
    project = db.get_project(user.id, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")
    deleted = db.delete_memory_record(user.id, memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="memory not found")
    memory_index.delete_memory(memory_id)


@router.post("/projects/{project_id}/memories", status_code=201)
async def create_memory(
    project_id: str,
    body: CreateMemoryRequest,
    user: User = Depends(current_user),
) -> MemoryResponse:
    project = db.get_project(user.id, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="project not found")

    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="content cannot be empty")
    if len(content) > 32_000:
        raise HTTPException(status_code=400, detail="content too long (max 32 000 chars)")

    try:
        tier = MemoryTier(body.tier)
        role = MessageRole(body.role)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    mem = Memory(user_id=user.id, project_id=project_id, tier=tier, role=role, content=content)
    db.save_memory(mem)
    memory_index.upsert_memory(
        memory_id=mem.id, text=content, project_id=project_id, user_id=user.id,
        tier=tier.value, role=role.value, created_at=mem.created_at,
    )
    return _to_response(mem)
