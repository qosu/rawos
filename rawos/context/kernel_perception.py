"""rawos/context/kernel_perception.py — Phase 24a: Being's kernel-level senses.

Reads kernel events (process exec, outbound TCP connect) machine-wide via the
arch KernelObserver (bpftrace subprocess emitting JSON lines) and persists
them to context_events under RAWOS_ENTITY_USER_ID (source_type="kernel").

Perception-only: events are never routed to agent action in this phase.
Dormant by default (ebpf_perception_enabled=False).
Mirrors context/system_perception.py (Phase 20): exclude → debounce →
classify → persist, errors logged-never-propagated.
"""
from __future__ import annotations

import asyncio
import logging
import time

from rawos.config import settings
from rawos.context.collector import _record_event
from rawos.kernel.arch import get_arch
from rawos.kernel.entity import RAWOS_ENTITY_USER_ID

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event types / severities — no magic numbers
# ---------------------------------------------------------------------------
EVENT_TYPE_KERNEL_EXEC: str = "kernel_process_exec"
EVENT_TYPE_KERNEL_NET: str = "kernel_network_connect"

SEVERITY_EXEC_DEFAULT: int = 2
SEVERITY_EXEC_SENSITIVE: int = 5
SEVERITY_NET_DEFAULT: int = 2

# Exec of these binaries (basename) is a higher-severity signal —
# privilege/account/credential changes worth the being's attention.
_SENSITIVE_EXEC_BASENAMES: tuple[str, ...] = (
    "sudo", "su", "passwd", "ssh", "useradd", "userdel", "visudo",
)

# Process exit / wait4 reaping — irrelevant to perception, dropped early.
_KNOWN_EVENT_TYPES: tuple[str, ...] = ("execve", "tcp_connect")


def _should_exclude(event: dict) -> bool:
    """Return True if this event must NOT be persisted.

    Drops events from comms on the owner-tunable denylist
    (settings.ebpf_perception_comm_denylist) — e.g. the being's own
    process names, or high-frequency daemons the owner wants silenced.
    """
    comm = event.get("comm")
    if comm in settings.ebpf_perception_comm_denylist:
        return True
    return False


def _classify(event: dict) -> tuple[str, int]:
    """Return (event_type, severity) for a parsed kernel event.

    execve of a sensitive binary (basename of `path`) is high severity;
    all other execve and tcp_connect events are default severity.
    """
    raw_type = event.get("event_type")

    if raw_type == "execve":
        path = event.get("path") or ""
        basename = path.rsplit("/", 1)[-1]
        if basename in _SENSITIVE_EXEC_BASENAMES:
            return EVENT_TYPE_KERNEL_EXEC, SEVERITY_EXEC_SENSITIVE
        return EVENT_TYPE_KERNEL_EXEC, SEVERITY_EXEC_DEFAULT

    return EVENT_TYPE_KERNEL_NET, SEVERITY_NET_DEFAULT


def _format_network_target(event: dict) -> str:
    """Return "daddr:dport" for a tcp_connect event."""
    return f"{event.get('daddr')}:{event.get('dport')}"


# ---------------------------------------------------------------------------
# Module-level state (debounce cache, mirrors _SystemHandler pattern)
# ---------------------------------------------------------------------------
_debounce_cache: dict[tuple[str, str], float] = {}


def _handle(event: dict) -> None:
    """Core dispatch: exclude → debounce → classify → persist.

    Errors in _record_event are caught and logged — never propagated.
    """
    if event.get("event_type") not in _KNOWN_EVENT_TYPES:
        return

    if _should_exclude(event):
        return

    comm = event.get("comm")
    event_type_raw = event.get("event_type")
    key = (comm, event_type_raw)

    now = time.monotonic()
    last = _debounce_cache.get(key, 0.0)
    if now - last < settings.ebpf_perception_debounce_s:
        return  # within debounce window — coalesce burst

    _debounce_cache[key] = now

    event_type, severity = _classify(event)
    if event_type_raw == "execve":
        path = event.get("path")
    else:
        path = _format_network_target(event)

    try:
        _record_event(
            RAWOS_ENTITY_USER_ID,
            event_type,
            path,
            {
                "source_type": "kernel",
                "severity": severity,
                "comm": comm,
                "pid": event.get("pid"),
            },
        )
    except Exception:
        _log.exception(
            "kernel_perception: failed to record event type=%s path=%s",
            event_type, path,
        )


async def _run_probe_cycle(observer) -> None:
    """Spawn the kernel observer's probe subprocess and consume its stdout.

    Async-iterates stdout line-by-line, parsing each via observer.parse_event
    and dispatching through _handle. The subprocess is terminated on exit
    (including cancellation) — never leaves an orphan.
    """
    proc = await asyncio.create_subprocess_exec(
        *observer.probe_command(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            event = observer.parse_event(line)
            if event is None:
                continue
            _handle(event)
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()


async def kernel_perception_loop() -> None:
    """Top-level loop for the being's kernel-level perception (Phase 24a).

    No-op if disabled by config or unsupported on this OS (dormant by
    default). Otherwise runs _run_probe_cycle forever, respawning the
    probe subprocess with a backoff if it exits or raises — never dies.
    """
    if not settings.ebpf_perception_enabled:
        return

    backend = get_arch()
    if not backend.kernel_observer.supports_kernel_observation:
        return

    while True:
        try:
            await _run_probe_cycle(backend.kernel_observer)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("kernel_perception: probe cycle failed, respawning")
        await asyncio.sleep(settings.ebpf_perception_respawn_backoff_s)
