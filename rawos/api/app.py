"""rawos FastAPI application — entry point."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from rawos.middleware.rate_limiter import RateLimiterMiddleware
from rawos.monitoring import MetricsMiddleware

import rawos.db as db
from rawos.config import settings
from rawos.api.auth_routes    import router as auth_router
from rawos.api.project_routes import router as project_router
from rawos.api.intent_routes  import router as intent_router
from rawos.api.file_routes    import router as file_router
from rawos.api.memory_routes  import router as memory_router
from rawos.api.agent_routes    import router as agent_router
from rawos.api.admin_routes    import router as admin_router
from rawos.api.billing_routes  import router as billing_router
from rawos.api.context_routes     import router as context_router
from rawos.api.evaluation_routes  import router as evaluation_router
from rawos.api.dataset_routes     import router as dataset_router
from rawos.api.classifier_routes  import router as classifier_router
from rawos.api.timing_routes      import router as timing_router
from rawos.api.study_routes       import router as study_router
from rawos.api.trust_routes       import router as trust_router
from rawos.api.calendar_routes    import router as calendar_router
from rawos.api.push_routes        import router as push_router

_log = logging.getLogger("rawos.startup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init(settings.db_path)
    Path(settings.workspaces_root).mkdir(parents=True, exist_ok=True)
    Path(settings.chroma_path).mkdir(parents=True, exist_ok=True)

    if settings.jwt_secret == "CHANGE_ME_IN_PRODUCTION":
        raise RuntimeError("FATAL: JWT_SECRET is still the default.")
    if not settings.debug and len(settings.jwt_secret) < 32:
        _log.warning("JWT_SECRET shorter than 32 chars — use a longer secret in production.")

    # Pre-warm ChromaDB + sentence-transformers
    from rawos.kernel.memory_index import warmup
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, warmup)

    # Start filesystem watcher (inotify on workspaces)
    from rawos.context.collector import start_filesystem_watcher, stop_filesystem_watcher, db_sync_loop
    start_filesystem_watcher()

    # Start background tasks
    db_sync_task       = asyncio.create_task(db_sync_loop(interval_s=30.0),       name="context-db-sync")
    proactive_task     = asyncio.create_task(_start_proactive_scheduler(),         name="proactive-scheduler")
    watcher_task       = asyncio.create_task(_personal_watcher_reload_loop(),     name="personal-watcher-reload")
    snapshot_task      = asyncio.create_task(_daily_snapshot_loop(),              name="study-daily-snapshot")
    calendar_task      = asyncio.create_task(_calendar_sync_loop_task(),          name="calendar-sync")
    autonomous_task    = asyncio.create_task(_start_autonomous_scan(),             name="autonomous-server-scan")
    self_probe_task    = asyncio.create_task(_start_self_probe_loop(),             name="rawos-self-probe")

    # Clean up intents orphaned by crash/restart — any still 'executing' after
    # MAX_PROACTIVE_LOOP_TIME_S+60s could not have been completed normally.
    import time as _time
    _orphan_cutoff = int(_time.time()) - 360  # 300s timeout + 60s buffer
    with db._conn() as _oc:
        _oc.execute(
            "UPDATE intents SET status='failed' WHERE status='executing' AND created_at < ?",
            (_orphan_cutoff,),
        )
    _log.info("rawos started — context collection active, proactive scheduler running, autonomous scan active")

    yield

    # Shutdown
    db_sync_task.cancel()
    proactive_task.cancel()
    watcher_task.cancel()
    snapshot_task.cancel()
    calendar_task.cancel()
    autonomous_task.cancel()
    self_probe_task.cancel()
    await asyncio.gather(db_sync_task, proactive_task, watcher_task, snapshot_task, calendar_task, autonomous_task, self_probe_task, return_exceptions=True)
    stop_filesystem_watcher()
    _log.info("rawos shutdown complete")


async def _start_proactive_scheduler() -> None:
    # Load intent classifier if available (Phase 9)
    from rawos.inference.intent_engine import load_classifier
    load_classifier()

    # Personal filesystem watcher (Phase 11)
    from rawos.context.collector import reload_personal_watcher
    reload_personal_watcher()

    from rawos.scheduler.proactive import proactive_scan_loop
    await proactive_scan_loop()


async def _start_autonomous_scan() -> None:
    from rawos.scheduler.proactive import autonomous_server_scan_loop
    await autonomous_server_scan_loop()


async def _start_self_probe_loop() -> None:
    from rawos.scheduler.proactive import rawos_self_probe_loop
    await rawos_self_probe_loop()


async def _personal_watcher_reload_loop() -> None:
    from rawos.context.collector import reload_personal_watcher
    while True:
        try:
            await asyncio.sleep(60)
            reload_personal_watcher()
        except asyncio.CancelledError:
            break
        except Exception:
            pass


async def _calendar_sync_loop_task() -> None:
    from rawos.context.calendar import calendar_sync_loop
    await calendar_sync_loop()


async def _daily_snapshot_loop() -> None:
    from datetime import datetime, timezone
    while True:
        try:
            await asyncio.sleep(3600)
            now = datetime.now(timezone.utc)
            if now.hour == 0:
                from rawos.study.tracker import take_daily_snapshot
                take_daily_snapshot()
        except asyncio.CancelledError:
            break
        except Exception:
            pass


app = FastAPI(
    title="rawos",
    version="0.6.0",
    docs_url="/docs" if settings.debug else None,
    redoc_url=None,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
    allow_credentials=True,
)
app.add_middleware(RateLimiterMiddleware)
app.add_middleware(MetricsMiddleware)

app.include_router(auth_router,    prefix="/auth",     tags=["auth"])
app.include_router(project_router, prefix="/projects", tags=["projects"])
app.include_router(intent_router,  prefix="/intent",   tags=["intent"])
app.include_router(file_router,    prefix="",          tags=["files"])
app.include_router(memory_router,  prefix="",          tags=["memories"])
app.include_router(agent_router,   prefix="",          tags=["agents"])
app.include_router(admin_router,              tags=["admin"])
app.include_router(billing_router,            tags=["billing"])
app.include_router(context_router,    tags=["context"])
app.include_router(evaluation_router, tags=["evaluation"])
app.include_router(dataset_router,    tags=["dataset"])
app.include_router(classifier_router, tags=["classifier"])
app.include_router(timing_router,     tags=["timing"])
app.include_router(study_router,      tags=["study"])
app.include_router(trust_router,    prefix="/trust",    tags=["trust"])
app.include_router(calendar_router, prefix="/calendar", tags=["calendar"])
app.include_router(push_router,     prefix="",          tags=["push"])


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.6.0", "phase": 11}


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request):
    from starlette.responses import Response as StarletteResponse
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

    client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if not client_ip:
        client_ip = request.client.host if request.client else ""

    is_localhost = client_ip in ("127.0.0.1", "::1", "")
    if not is_localhost:
        if not settings.metrics_token:
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="metrics not accessible remotely")
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {settings.metrics_token}":
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="invalid metrics token")

    return StarletteResponse(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
