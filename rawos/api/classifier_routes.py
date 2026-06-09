"""
rawos Classifier API Routes — Phase 9.

POST /classifier/train   — train on current labeled_examples, save model
GET  /classifier/status  — model loaded, metadata
GET  /classifier/benchmark — run or return cached benchmark results
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Query

from rawos.api.deps import current_user
from rawos.models import User

log = logging.getLogger("rawos.api.classifier")
router = APIRouter(prefix="/classifier")

_train_lock = asyncio.Lock()


@router.get("/status")
def classifier_status() -> dict:
    """Return classifier load status and metadata."""
    import rawos.inference.intent_engine as engine
    clf = engine._CLASSIFIER
    if clf is None:
        return {"loaded": False, "message": "no classifier trained yet"}
    return {
        "loaded": True,
        "model_type": clf.model_type,
        "cv_f1_mean": round(clf.cv_f1_mean, 4),
        "cv_f1_std":  round(clf.cv_f1_std,  4),
        "training_size": clf.training_size,
        "trained_at": clf.trained_at,
        "cv_results": clf.cv_results,
    }


@router.post("/train")
async def classifier_train(user: User = Depends(current_user)) -> dict:
    """
    Train classifier on current labeled_examples dataset (auth required).
    Saves to disk and hot-swaps into the running inference engine.
    Locked: only one training run at a time.
    """
    if _train_lock.locked():
        return {"status": "already_running"}

    async with _train_lock:
        from rawos.inference.classifier import train
        import rawos.inference.intent_engine as engine

        log.info("classifier training triggered by user=%s", user.id[:8])
        clf = await asyncio.get_event_loop().run_in_executor(None, train, True)

        # Hot-swap into running engine
        engine._CLASSIFIER = clf
        log.info("classifier hot-swapped: type=%s cv_f1=%.4f", clf.model_type, clf.cv_f1_mean)

        return {
            "status": "ok",
            "model_type": clf.model_type,
            "cv_f1_mean": round(clf.cv_f1_mean, 4),
            "cv_f1_std":  round(clf.cv_f1_std,  4),
            "training_size": clf.training_size,
            "cv_results": clf.cv_results,
        }


@router.get("/benchmark")
async def classifier_benchmark(
    llm_sample: int = Query(default=0, ge=0, le=50,
                            description="Number of examples to evaluate with LLM (0=skip, expensive)"),
    force: bool = Query(default=False, description="Force re-run even if cached results exist"),
) -> dict:
    """
    Run full benchmark (rule vs classifier vs optional LLM sample).
    Returns cached results if available and force=False.
    """
    from rawos.inference.benchmark import run_full_benchmark, load_cached_results

    if not force and llm_sample == 0:
        cached = load_cached_results()
        if cached:
            return {"status": "cached", **cached}

    results = await run_full_benchmark(llm_sample=llm_sample)
    return {"status": "fresh", **results}
