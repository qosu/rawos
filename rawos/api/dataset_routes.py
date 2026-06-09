"""
rawos Dataset API Routes — Phase 8.

Provides read access to the labeled dataset and a build trigger.
POST /dataset/build is protected: requires auth token.
GET routes are open for research transparency.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.responses import FileResponse

from rawos.api.deps import current_user
from rawos.models import User

log = logging.getLogger("rawos.api.dataset")
router = APIRouter(prefix="/dataset")

_build_lock = asyncio.Lock()
_last_build_result: dict | None = None


@router.get("/stats")
def dataset_stats() -> dict:
    """Dataset statistics: total, by source, by domain."""
    from rawos.dataset.manager import stats
    return stats()


@router.get("/examples")
def dataset_examples(
    domain: str | None = Query(default=None),
    source: str | None = Query(default=None),
    limit: int = Query(default=50, le=500),
    offset: int = Query(default=0),
) -> dict:
    """List labeled examples with optional domain/source filter."""
    from rawos.dataset.manager import list_examples
    from rawos.dataset.schema import VALID_DOMAINS

    if domain and domain not in VALID_DOMAINS:
        return {"error": f"unknown domain {domain!r}", "valid_domains": sorted(VALID_DOMAINS)}

    examples = list_examples(domain=domain, source=source, limit=limit, offset=offset)
    return {
        "count": len(examples),
        "examples": [
            {
                "id": ex.id,
                "source": ex.source,
                "true_goal": ex.true_goal,
                "true_domain": ex.true_domain,
                "expected_confidence": ex.expected_confidence,
                "quality_score": ex.quality_score,
                "behavioral_context": ex.behavioral_context.to_dict(),
                "notes": ex.notes,
            }
            for ex in examples
        ],
    }


@router.post("/build")
async def dataset_build(
    extract: bool = Query(default=True),
    synthetic_per_domain: int = Query(default=8, ge=0, le=20),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    user: User = Depends(current_user),
) -> dict:
    """
    Trigger dataset build (auth required).
    Runs synchronously — returns when complete.
    Locked: only one build may run at a time.
    """
    global _last_build_result

    if _build_lock.locked():
        return {"status": "already_running", "message": "A build is already in progress."}

    async with _build_lock:
        from rawos.dataset.manager import build
        log.info("dataset build triggered by user=%s extract=%s synthetic_per_domain=%d",
                 user.id[:8], extract, synthetic_per_domain)
        result = await build(
            extract=extract,
            synthetic_per_domain=synthetic_per_domain,
        )
        _last_build_result = result
        return {"status": "complete", **result}


@router.get("/export")
def dataset_export(user: User = Depends(current_user)) -> FileResponse:
    """Export the full dataset as JSON lines (auth required)."""
    from rawos.dataset.manager import export_jsonl
    path, count = export_jsonl()
    return FileResponse(
        path=path,
        media_type="application/x-ndjson",
        filename="rawos_dataset.jsonl",
        headers={"X-Example-Count": str(count)},
    )
