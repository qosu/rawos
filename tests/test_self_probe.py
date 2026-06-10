"""
Phase 16 step d — the self-probe loop ships DORMANT (commit 552b752e).
settings.self_probe_enabled defaults to False; while disabled,
rawos_self_probe_loop() logs once and returns immediately — no loop,
no sleep, no worktree side effects. See PLAN.md Pass 2 — IMPLEMENTED.
"""
import asyncio

from rawos.config import settings
from rawos.scheduler.proactive import rawos_self_probe_loop


def test_self_probe_disabled_by_default():
    assert settings.self_probe_enabled is False


def test_self_probe_loop_returns_immediately_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "self_probe_enabled", False)

    async def _run_with_timeout():
        await asyncio.wait_for(rawos_self_probe_loop(), timeout=2.0)

    asyncio.run(_run_with_timeout())
