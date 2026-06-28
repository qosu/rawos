"""anima FastAPI application — entry point."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from anima.middleware.rate_limiter import RateLimiterMiddleware
from anima.monitoring import MetricsMiddleware

import anima.db as db
from anima.config import settings
from anima.kernel.telegram_gate import TelegramGate
from anima.api.auth_routes    import router as auth_router
from anima.api.project_routes import router as project_router
from anima.api.intent_routes  import router as intent_router
from anima.api.file_routes    import router as file_router
from anima.api.memory_routes  import router as memory_router
from anima.api.agent_routes    import router as agent_router
from anima.api.admin_routes    import router as admin_router
from anima.api.billing_routes  import router as billing_router
from anima.api.context_routes     import router as context_router
from anima.api.evaluation_routes  import router as evaluation_router
from anima.api.dataset_routes     import router as dataset_router
from anima.api.classifier_routes  import router as classifier_router
from anima.api.timing_routes      import router as timing_router
from anima.api.study_routes       import router as study_router
from anima.api.trust_routes       import router as trust_router
from anima.api.calendar_routes    import router as calendar_router
from anima.api.push_routes        import router as push_router

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:%(name)s:%(message)s",
)
_log = logging.getLogger("anima.startup")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init(settings.db_path)
    Path(settings.workspaces_root).mkdir(parents=True, exist_ok=True)
    Path(settings.chroma_path).mkdir(parents=True, exist_ok=True)

    if settings.jwt_secret == "CHANGE_ME_IN_PRODUCTION":
        raise RuntimeError("FATAL: JWT_SECRET is still the default.")
    if not settings.debug and len(settings.jwt_secret) < 32:
        _log.warning("JWT_SECRET shorter than 32 chars — use a longer secret in production.")

    # Phase 26 -- I-LL4 fail-fast: if Landlock self-MAC is enabled but this
    # kernel's ABI is below MIN_ABI, refuse to boot rather than silently run
    # every run_bash call unsandboxed while claiming to be sandboxed.
    from anima.kernel import landlock
    landlock.validate_boot_config(enabled=settings.landlock_self_mac_enabled)

    # Phase 24B — fail-fast: refuse boot if bpf_lsm is enabled but BPF LSM is
    # not available (bpf absent from active LSM list, BTF missing, or bpffs
    # unmounted). Mirrors Phase 26 Landlock validate_boot_config (I-LSM10/I-LSM12).
    from anima.kernel import bpf_lsm as _bpf_lsm_mod
    _bpf_lsm_mod.validate_boot_config(
        enabled=settings.bpf_lsm_enabled,
        mode=settings.bpf_lsm_mode,
    )

    # Phase 23-full — fail-fast: refuse boot if unit topology enabled but
    # systemd not available as PID1, or default.target outside allowed set (I-UT9/I-UT11).
    # enabled=False (I-UT11 dormant default) → no-op, never raises.
    from anima.kernel import unit_topology as _unit_topology_mod
    _unit_topology_mod.validate_boot_config(
        enabled=settings.operator_unit_topology_enabled,
    )

    # Pre-warm ChromaDB + sentence-transformers
    from anima.kernel.memory_index import warmup
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, warmup)

    # Start filesystem watcher (inotify on workspaces)
    from anima.context.collector import start_filesystem_watcher, stop_filesystem_watcher, db_sync_loop
    start_filesystem_watcher()

    # Phase 20 — being's real-time system perception (dormant until system_perception_enabled=True)
    from anima.context.system_perception import start_system_perception, stop_system_perception
    start_system_perception()

    # Start background tasks
    db_sync_task       = asyncio.create_task(db_sync_loop(interval_s=30.0),       name="context-db-sync")
    proactive_task     = asyncio.create_task(_start_proactive_scheduler(),         name="proactive-scheduler")
    watcher_task       = asyncio.create_task(_personal_watcher_reload_loop(),     name="personal-watcher-reload")
    snapshot_task      = asyncio.create_task(_daily_snapshot_loop(),              name="study-daily-snapshot")
    calendar_task      = asyncio.create_task(_calendar_sync_loop_task(),          name="calendar-sync")
    autonomous_task    = asyncio.create_task(_start_autonomous_scan(),             name="autonomous-server-scan")
    self_probe_task    = asyncio.create_task(_start_self_probe_loop(),             name="anima-self-probe")
    narrative_task     = asyncio.create_task(_start_narrative_consolidation_loop(), name="narrative-consolidation")
    operator_scan_task       = asyncio.create_task(_start_operator_scan_loop(),          name="operator-scan")
    system_fs_reflex_task = asyncio.create_task(_start_system_fs_reflex(),              name="system-fs-reflex")
    kernel_perception_task = asyncio.create_task(_start_kernel_perception_loop(),       name="kernel-perception")
    selfreload_task    = asyncio.create_task(_self_reload_boot_commit_task(),       name="self-reload-boot-commit")
    venv_boot_task     = asyncio.create_task(_venv_boot_commit_task(),                name="venv-boot-commit")
    # Phase 24B — heartbeat supervisor (I-LSM7 deadman). No-op when dormant
    # (bpf_lsm_enabled=False → BpfLsmSupervisor.run() returns immediately).
    bpf_lsm_heartbeat_task = asyncio.create_task(_start_bpf_lsm_heartbeat_loop(),    name="bpf-lsm-heartbeat")
    audit_mirror_task  = asyncio.create_task(_audit_mirror_loop(),                          name="audit-chain-mirror")
    _telegram_gate     = await _start_telegram_gate()

    # Clean up intents orphaned by crash/restart — any still 'executing' after
    # MAX_PROACTIVE_LOOP_TIME_S+60s could not have been completed normally.
    import time as _time
    _orphan_cutoff = int(_time.time()) - 360  # 300s timeout + 60s buffer
    with db._conn() as _oc:
        _oc.execute(
            "UPDATE intents SET status='failed' WHERE status='executing' AND created_at < ?",
            (_orphan_cutoff,),
        )
    _log.info("anima started — context collection active, proactive scheduler running, autonomous scan active")

    # SHP.5 I-SEC7: audit chain boot verify + startup record
    from anima.kernel import audit_chain as _audit_chain_mod
    _boot_verify = _audit_chain_mod.verify_chain()
    if not _boot_verify.ok:
        _log.error(
            "SHP.5: audit chain tamper detected (%d records checked): %s",
            _boot_verify.records_verified, _boot_verify.reason,
        )
    else:
        _log.info(
            "SHP.5: audit chain OK (%d records verified)",
            _boot_verify.records_verified,
        )
    # SHP.7 I-SEC9: dep drift check — detect supply-chain version tampering at boot
    from anima.kernel.dep_lock import verify_dep_lock as _verify_dep_lock
    _dep_drift = _verify_dep_lock()
    if not _dep_drift.ok:
        _log.warning(
            "SHP.7: dep lock drift detected at boot: %s | added=%s removed=%s changed=%s",
            _dep_drift.summary,
            _dep_drift.added[:5],
            _dep_drift.removed[:5],
            _dep_drift.changed[:5],
        )
    else:
        _log.info("SHP.7: dep lock OK")

    _audit_chain_mod.append(
        "startup",
        {
            "chain_records_at_boot": _boot_verify.records_verified,
            "chain_ok_at_boot": _boot_verify.ok,
            "dep_lock_ok_at_boot": _dep_drift.ok,
            "dep_drift_summary": _dep_drift.summary,
        },
    )

    yield

    # Shutdown
    if _telegram_gate is not None:
        await _telegram_gate.stop()
    db_sync_task.cancel()
    proactive_task.cancel()
    watcher_task.cancel()
    snapshot_task.cancel()
    calendar_task.cancel()
    autonomous_task.cancel()
    self_probe_task.cancel()
    narrative_task.cancel()
    operator_scan_task.cancel()
    system_fs_reflex_task.cancel()
    kernel_perception_task.cancel()
    selfreload_task.cancel()
    venv_boot_task.cancel()
    bpf_lsm_heartbeat_task.cancel()
    audit_mirror_task.cancel()
    await asyncio.gather(db_sync_task, proactive_task, watcher_task, snapshot_task, calendar_task, autonomous_task, self_probe_task, narrative_task, operator_scan_task, system_fs_reflex_task, kernel_perception_task, selfreload_task, venv_boot_task, bpf_lsm_heartbeat_task, audit_mirror_task, return_exceptions=True)
    stop_system_perception()
    stop_filesystem_watcher()
    _log.info("anima shutdown complete")


async def _start_proactive_scheduler() -> None:
    # Load intent classifier if available (Phase 9)
    from anima.inference.intent_engine import load_classifier
    load_classifier()

    # Personal filesystem watcher (Phase 11)
    from anima.context.collector import reload_personal_watcher
    reload_personal_watcher()

    from anima.scheduler.proactive import proactive_scan_loop
    await proactive_scan_loop()


async def _start_system_fs_reflex() -> None:
    from anima.scheduler.system_reflex import system_fs_reflex_loop
    await system_fs_reflex_loop()


async def _start_kernel_perception_loop() -> None:
    from anima.context.kernel_perception import kernel_perception_loop
    await kernel_perception_loop()


async def _start_bpf_lsm_heartbeat_loop() -> None:
    """Phase 24B: BPF LSM heartbeat supervisor (I-LSM7 deadman loop).

    When bpf_lsm_enabled=False (24B.0 dormant), run() returns immediately.
    When enabled=True (post-24B.1), uses _SocketHolderClient to send
    periodic heartbeats; missing heartbeats trigger holder self-detach
    (enforcement revert without reboot, I-LSM2).
    """
    from anima.kernel import bpf_lsm as _bpf_lsm_mod
    if settings.bpf_lsm_enabled:
        client: _bpf_lsm_mod.BpfLsmHolderClient = _bpf_lsm_mod._SocketHolderClient()
        # SHP.6: apply configured mode at startup (I-LSM4 — was never wired before)
        try:
            await client.flip_mode(settings.bpf_lsm_mode)
            _log.info(
                "SHP.6: BPF LSM mode initialized to %r at startup", settings.bpf_lsm_mode
            )
        except Exception:
            _log.warning(
                "SHP.6: BPF LSM startup mode flip to %r failed "
                "(holder socket may not be ready — will retry on next heartbeat cycle)",
                settings.bpf_lsm_mode,
            )
    else:
        client = _bpf_lsm_mod._NullHolderClient()
    supervisor = _bpf_lsm_mod.BpfLsmSupervisor(
        client=client,
        heartbeat_interval_s=5.0,
        enabled=settings.bpf_lsm_enabled,
    )
    await supervisor.run()



async def _audit_mirror_loop() -> None:
    """SHP.5: periodically push chain-head to Telegram as off-box anchor."""
    from anima.kernel import audit_chain as _ac
    while True:
        await asyncio.sleep(_ac.MIRROR_INTERVAL_S)
        try:
            await _ac.push_mirror()
        except Exception:
            _log.exception("audit chain mirror push failed")


async def _self_reload_boot_commit_task() -> None:
    """Resolve any pending self-reload from a prior `anima selfreload arm-and-go`.

    Phase 25 Stage 1 (dormant unless an arm-and-go is in flight — see
    kernel/self_reload.py). Runs once at boot, AFTER this point in lifespan
    so the ASGI app can already accept requests and the probe below can hit
    its own /health over loopback (calling boot_liveness_commit() directly
    inside startup would deadlock: no connections are accepted until
    lifespan startup returns).

    On "committed"/"resurrected"/"liveness_failed" the outcome (with the
    old/new SHAs read from the pending state file before it is consumed) is
    appended to the managed_self_reload ledger for `anima selfreload status`
    and Stage 2's future graduation check. Never raises — a failure here must
    not prevent anima from serving.
    """
    import json as _json
    from pathlib import Path as _Path

    import httpx

    import time as _time

    from anima.kernel.entity import RAWOS_ENTITY_USER_ID
    from anima.kernel.self_reload import (
        SELF_RELOAD_STATE_DIR,
        SELF_RELOAD_STATE_FILENAME,
        SOURCE_ROOT,
        boot_liveness_commit,
    )

    state_path = _Path(SELF_RELOAD_STATE_DIR) / SELF_RELOAD_STATE_FILENAME
    if not state_path.exists():
        return

    try:
        pending = _json.loads(state_path.read_text())
        old_sha, new_sha = pending["old_sha"], pending["new_sha"]
        autonomous = bool(pending.get("autonomous", False))
    except Exception:
        _log.exception("self-reload: pending.json unreadable — leaving deadman armed")
        return

    def _probe() -> bool:
        try:
            resp = httpx.get(f"http://127.0.0.1:{settings.port}/health", timeout=2.0)
            return resp.status_code == 200
        except Exception:
            return False

    loop = asyncio.get_event_loop()
    try:
        outcome = await loop.run_in_executor(None, lambda: boot_liveness_commit(_probe=_probe))
    except Exception:
        _log.exception("self-reload: boot_liveness_commit failed — leaving deadman armed")
        return

    if outcome == "no_pending":
        return

    _log.info("self-reload: boot_liveness_commit -> %s (old=%s new=%s autonomous=%s)", outcome, old_sha, new_sha, autonomous)
    try:
        db.record_self_reload_outcome(old_sha, new_sha, outcome, autonomous=autonomous)
    except Exception:
        _log.exception("self-reload: failed to record outcome %s in ledger", outcome)

    # I-SR11: update graduation ledger so operate_on_self_reload() can check readiness.
    try:
        db.update_operator_track_record(
            RAWOS_ENTITY_USER_ID,
            "self_reload",
            SOURCE_ROOT,
            verified=(outcome == "committed"),
            now=int(_time.time()),
        )
    except Exception:
        _log.exception("self-reload: failed to update track record for outcome %s", outcome)



async def _venv_boot_commit_task() -> None:
    """Resolve any pending venv swap from a prior `rawos-venv-revert` arm.

    M3 Stage 2 (dormant unless arm_and_swap_venv is in flight — see
    kernel/venv_operator.py). Runs once at boot, AFTER the ASGI app is
    accepting requests so the /health probe below succeeds.

    On "committed"/"liveness_failed" the outcome is appended to the
    venv_operator_history ledger. Never raises — failure must not prevent
    anima from serving.
    """
    import json as _json
    from pathlib import Path as _Path

    import httpx

    from anima.kernel.venv_operator import (
        VENV_STATE_DIR,
        VENV_STATE_FILENAME,
        boot_venv_commit,
    )

    state_path = _Path(VENV_STATE_DIR) / VENV_STATE_FILENAME
    if not state_path.exists():
        return

    try:
        pending = _json.loads(state_path.read_text())
        frozen_before = pending.get("frozen_hash_before", "")
        frozen_after = pending.get("frozen_hash_after", "")
    except Exception:
        _log.exception("venv-boot-commit: state.json unreadable — leaving deadman armed")
        return

    def _probe() -> bool:
        try:
            resp = httpx.get(f"http://127.0.0.1:{settings.port}/health", timeout=2.0)
            return resp.status_code == 200
        except Exception:
            return False

    loop = asyncio.get_event_loop()
    try:
        outcome = await loop.run_in_executor(None, lambda: boot_venv_commit(_probe=_probe))
    except Exception:
        _log.exception("venv-boot-commit: boot_venv_commit raised — leaving deadman armed")
        return

    if outcome == "no_pending":
        return

    _log.info("venv-boot-commit: outcome=%s frozen_before=%s frozen_after=%s",
              outcome, frozen_before[:16], frozen_after[:16])
    try:
        db.record_venv_op_outcome(
            op_type="dep_update",
            frozen_hash_before=frozen_before,
            frozen_hash_after=frozen_after,
            outcome=outcome if outcome in ("applied", "proposed", "liveness_failed", "preflight_failed") else "liveness_failed",
            autonomous=False,
        )
    except Exception:
        _log.exception("venv-boot-commit: failed to record outcome %s in ledger", outcome)

async def _start_autonomous_scan() -> None:
    from anima.scheduler.proactive import autonomous_server_scan_loop
    await autonomous_server_scan_loop()


async def _start_self_probe_loop() -> None:
    from anima.scheduler.proactive import anima_self_probe_loop
    await anima_self_probe_loop()


async def _start_narrative_consolidation_loop() -> None:
    from anima.scheduler.proactive import anima_narrative_consolidation_loop
    await anima_narrative_consolidation_loop()


async def _start_operator_scan_loop() -> None:
    from anima.scheduler.proactive import anima_operator_scan_loop
    await anima_operator_scan_loop()


async def _personal_watcher_reload_loop() -> None:
    from anima.context.collector import reload_personal_watcher
    while True:
        try:
            await asyncio.sleep(60)
            reload_personal_watcher()
        except asyncio.CancelledError:
            break
        except Exception:
            pass


async def _calendar_sync_loop_task() -> None:
    from anima.context.calendar import calendar_sync_loop
    await calendar_sync_loop()


async def _daily_snapshot_loop() -> None:
    from datetime import datetime, timezone
    while True:
        try:
            await asyncio.sleep(3600)
            now = datetime.now(timezone.utc)
            if now.hour == 0:
                from anima.study.tracker import take_daily_snapshot
                take_daily_snapshot()
        except asyncio.CancelledError:
            break
        except Exception:
            pass


async def _start_telegram_gate():
    """Start Telegram polling gate if telegram_enabled=True and token is set.

    Returns the running TelegramGate instance, or None if disabled/misconfigured.
    """
    if not settings.telegram_enabled:
        return None
    if not settings.telegram_bot_token:
        _log.warning("telegram_enabled=True but telegram_bot_token is empty — gate not started")
        return None
    gate = TelegramGate(
        bot_token=settings.telegram_bot_token,
        owner_chat_id=settings.telegram_owner_chat_id,
        owner_email=settings.telegram_owner_email,
        project_id=settings.telegram_project_id,
    )
    await gate.start()
    return gate


app = FastAPI(
    title="anima",
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


@app.post("/internal/self-reload/arm-and-go", include_in_schema=False)
async def internal_self_reload_arm_and_go(request: Request):
    """Owner-triggered self-reload (Phase 25 I-SR6 funnel), loopback-only.

    Must run IN-PROCESS: execute_owner_self_reload()'s os._exit(0) has to
    kill THIS worker's MainPID -- that is the only way systemd
    (Restart=always) respawns anima.service against new_sha and
    boot_liveness_commit (lifespan, above) can resolve the pending state
    written here. See anima/cli/main.py `selfreload arm-and-go`.
    """
    from fastapi import HTTPException

    client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if not client_ip:
        client_ip = request.client.host if request.client else ""
    if client_ip not in ("127.0.0.1", "::1", ""):
        raise HTTPException(status_code=403, detail="self-reload not accessible remotely")

    body = await request.json()
    new_sha = body.get("new_sha", "")
    if not new_sha:
        raise HTTPException(status_code=400, detail="new_sha required")

    from anima.kernel.self_reload import (
        SelfReloadPreflightError,
        SelfReloadRefusalError,
        SelfReloadStateError,
        execute_owner_self_reload,
    )

    try:
        execute_owner_self_reload(new_sha)
    except (SelfReloadRefusalError, SelfReloadPreflightError, SelfReloadStateError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    # execute_owner_self_reload calls os._exit(0) on success -- unreachable
    # in production. Reached only when a test monkeypatches it.
    return {"status": "armed"}


@app.post("/internal/self-reload/_debug-arm-and-swap", include_in_schema=False)
async def internal_self_reload_debug_arm_and_swap(request: Request):
    """Phase 25 twin-prove ONLY -- 404 unless
    settings.self_reload_debug_endpoint_enabled (twin .env only, default False).

    Unlike /internal/self-reload/arm-and-go (I-SR6 owner funnel, which calls
    execute_owner_self_reload -> arm_and_swap with the DEFAULT revert_cmd
    /usr/local/bin/rawos-selfreload-revert), this calls preflight_stage +
    arm_and_swap directly with _revert_cmd overridden to
    /usr/local/bin/rawos-selfprobe-revert. The prod revert script hardcodes
    REPO=/root/rawos + `systemctl restart anima` -- armed by a twin process
    (whose old_sha is a real commit in prod's history too, since the twin is
    a clone of prod), its deadman firing would reset PROD's repo and restart
    anima.service. _revert_cmd injection keeps the twin's deadman scoped to
    /root/rawos-selfprobe-tree + anima-selfprobe.
    """
    from fastapi import HTTPException

    if not settings.self_reload_debug_endpoint_enabled:
        raise HTTPException(status_code=404)

    client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if not client_ip:
        client_ip = request.client.host if request.client else ""
    if client_ip not in ("127.0.0.1", "::1", ""):
        raise HTTPException(status_code=403, detail="self-reload not accessible remotely")

    body = await request.json()
    new_sha = body.get("new_sha", "")
    if not new_sha:
        raise HTTPException(status_code=400, detail="new_sha required")

    from anima.kernel.self_reload import (
        SelfReloadPreflightError,
        SelfReloadRefusalError,
        SelfReloadStateError,
        arm_and_swap,
        preflight_stage,
    )

    try:
        snap = preflight_stage(new_sha)
    except (SelfReloadRefusalError, SelfReloadPreflightError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    revert_cmd = f"/usr/local/bin/rawos-selfprobe-revert {snap.old_sha} {snap.state_id}"
    try:
        arm_and_swap(snap, _revert_cmd=revert_cmd)
    except SelfReloadStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    # arm_and_swap calls os._exit(0) on success -- unreachable in production.
    # Reached only when a test monkeypatches it.
    return {"status": "armed"}
