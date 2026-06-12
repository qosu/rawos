"""
rawos Context API routes.

GET  /context/status    — current user model + inferred intent
GET  /context/goals     — recent proactive artifacts (what rawos has done)
POST /context/infer     — force intent re-inference (LLM, not cached)
GET  /context/why       — explain why a file was created (query param: path)
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from rawos.api.deps import current_user as get_current_user
from rawos.context.user_model import get_user_model, rebuild_user_model
from rawos.inference.intent_engine import infer_intent
from rawos.manifester.writer import get_provenance, list_proactive_artifacts

router = APIRouter()


@router.get("/context/status")
async def context_status(user=Depends(get_current_user)):
    model = get_user_model(user.id)
    if not model:
        # Build it fresh from whatever events exist
        import asyncio
        model = await asyncio.get_event_loop().run_in_executor(
            None, rebuild_user_model, user.id
        )
    intent = await infer_intent(user.id)
    return {
        "user_id": user.id,
        "inferred_stack": model.get("inferred_stack", []),
        "active_domains": model.get("active_domains", []),
        "current_project_id": model.get("current_project_id"),
        "intent": {
            "goal": intent.goal,
            "confidence": round(intent.confidence, 3),
            "domain": intent.domain,
            "suggested_actions": intent.suggested_actions,
            "source": intent.source,
        },
        "event_count": model.get("event_count", 0),
    }


@router.get("/context/goals")
async def context_goals(
    limit: int = Query(default=20, ge=1, le=100),
    user=Depends(get_current_user),
):
    artifacts = list_proactive_artifacts(user.id, limit=limit)
    return {"proactive_artifacts": artifacts, "count": len(artifacts)}


@router.post("/context/infer")
async def context_infer_force(user=Depends(get_current_user)):
    """Force LLM re-inference, bypassing cache."""
    intent = await infer_intent(user.id, force_llm=True)
    return {
        "goal": intent.goal,
        "confidence": round(intent.confidence, 3),
        "domain": intent.domain,
        "suggested_actions": intent.suggested_actions,
        "source": intent.source,
    }


@router.get("/context/why")
async def context_why(
    path: str = Query(..., description="Absolute path to a RAWOS_ file"),
    user=Depends(get_current_user),
):
    prov = get_provenance(path)
    if not prov:
        raise HTTPException(status_code=404, detail="no provenance found for this file")
    return prov


@router.post("/context/session_start")
async def session_start(user=Depends(get_current_user)):
    """Return proactive artifacts since last chat, then update last_chat_at."""
    import time
    import rawos.db as _db

    last_chat_at = _db.get_last_chat_at(user.id)
    artifacts = _db.get_proactive_artifacts_since(user.id, since_ts=last_chat_at)
    _db.set_last_chat_at(user.id, int(time.time()))

    return {"last_chat_at": last_chat_at, "artifacts": artifacts}
