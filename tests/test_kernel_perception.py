"""tests/test_kernel_perception.py — TDD for the kernel perception loop (Phase 24a).

_record_event and the kernel observer's probe subprocess are mocked at their
boundaries. The probe script / subprocess mechanism itself was verified live
on the box (see plan); this exercises the pure parsing/dispatch/loop logic.
"""
from __future__ import annotations

import asyncio

import pytest

from rawos.context import kernel_perception as kp
from rawos.kernel.entity import RAWOS_ENTITY_USER_ID


# ---------------------------------------------------------------------------
# _classify
# ---------------------------------------------------------------------------

def test_classify_default_execve():
    event = {"event_type": "execve", "comm": "bash", "pid": 1, "path": "/bin/ls"}

    event_type, severity = kp._classify(event)

    assert event_type == kp.EVENT_TYPE_KERNEL_EXEC
    assert severity == kp.SEVERITY_EXEC_DEFAULT


def test_classify_sensitive_execve():
    event = {"event_type": "execve", "comm": "bash", "pid": 1, "path": "/usr/bin/sudo"}

    event_type, severity = kp._classify(event)

    assert event_type == kp.EVENT_TYPE_KERNEL_EXEC
    assert severity == kp.SEVERITY_EXEC_SENSITIVE


def test_classify_tcp_connect():
    event = {"event_type": "tcp_connect", "comm": "curl", "pid": 1, "daddr": "1.2.3.4", "dport": 443}

    event_type, severity = kp._classify(event)

    assert event_type == kp.EVENT_TYPE_KERNEL_NET
    assert severity == kp.SEVERITY_NET_DEFAULT


# ---------------------------------------------------------------------------
# _should_exclude
# ---------------------------------------------------------------------------

def test_should_exclude_denylisted_comm(monkeypatch):
    monkeypatch.setattr(kp.settings, "ebpf_perception_comm_denylist", ("bpftrace",))

    assert kp._should_exclude({"comm": "bpftrace"}) is True


def test_should_not_exclude_non_denylisted_comm(monkeypatch):
    monkeypatch.setattr(kp.settings, "ebpf_perception_comm_denylist", ("bpftrace",))

    assert kp._should_exclude({"comm": "bash"}) is False


# ---------------------------------------------------------------------------
# _handle — debounce + dispatch to _record_event
# ---------------------------------------------------------------------------

def test_handle_records_event(monkeypatch):
    kp._debounce_cache.clear()
    monkeypatch.setattr(kp.settings, "ebpf_perception_comm_denylist", ())
    monkeypatch.setattr(kp.settings, "ebpf_perception_debounce_s", 1000.0)

    recorded = []
    monkeypatch.setattr(kp, "_record_event", lambda *a, **kw: recorded.append((a, kw)))

    kp._handle({"event_type": "execve", "comm": "bash", "pid": 1, "path": "/bin/ls"})

    assert len(recorded) == 1
    (user_id, event_type, path, metadata), _ = recorded[0]
    assert user_id == RAWOS_ENTITY_USER_ID
    assert event_type == kp.EVENT_TYPE_KERNEL_EXEC
    assert path == "/bin/ls"
    assert metadata["source_type"] == "kernel"
    assert metadata["comm"] == "bash"
    assert metadata["pid"] == 1


def test_handle_debounces_duplicate_within_window(monkeypatch):
    kp._debounce_cache.clear()
    monkeypatch.setattr(kp.settings, "ebpf_perception_comm_denylist", ())
    monkeypatch.setattr(kp.settings, "ebpf_perception_debounce_s", 1000.0)

    recorded = []
    monkeypatch.setattr(kp, "_record_event", lambda *a, **kw: recorded.append((a, kw)))

    event = {"event_type": "execve", "comm": "bash", "pid": 1, "path": "/bin/ls"}
    kp._handle(event)
    kp._handle(event)

    assert len(recorded) == 1


def test_handle_excludes_denylisted_comm(monkeypatch):
    kp._debounce_cache.clear()
    monkeypatch.setattr(kp.settings, "ebpf_perception_comm_denylist", ("bash",))
    monkeypatch.setattr(kp.settings, "ebpf_perception_debounce_s", 1000.0)

    recorded = []
    monkeypatch.setattr(kp, "_record_event", lambda *a, **kw: recorded.append((a, kw)))

    kp._handle({"event_type": "execve", "comm": "bash", "pid": 1, "path": "/bin/ls"})

    assert recorded == []


def test_handle_drops_unknown_event_type(monkeypatch):
    kp._debounce_cache.clear()
    recorded = []
    monkeypatch.setattr(kp, "_record_event", lambda *a, **kw: recorded.append((a, kw)))

    kp._handle({"event_type": "attached_probes"})

    assert recorded == []


# ---------------------------------------------------------------------------
# kernel_perception_loop — gating
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_loop_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(kp.settings, "ebpf_perception_enabled", False)

    called = []
    monkeypatch.setattr(kp, "_run_probe_cycle", lambda *a, **kw: called.append(1))

    await kp.kernel_perception_loop()

    assert called == []


@pytest.mark.asyncio
async def test_loop_noop_when_observer_unsupported(monkeypatch):
    monkeypatch.setattr(kp.settings, "ebpf_perception_enabled", True)

    class _Unsupported:
        supports_kernel_observation = False

    class _Backend:
        kernel_observer = _Unsupported()

    monkeypatch.setattr(kp, "get_arch", lambda: _Backend())

    called = []
    monkeypatch.setattr(kp, "_run_probe_cycle", lambda *a, **kw: called.append(1))

    await kp.kernel_perception_loop()

    assert called == []


@pytest.mark.asyncio
async def test_loop_survives_probe_cycle_exception(monkeypatch):
    monkeypatch.setattr(kp.settings, "ebpf_perception_enabled", True)
    monkeypatch.setattr(kp.settings, "ebpf_perception_respawn_backoff_s", 0.0)

    class _Supported:
        supports_kernel_observation = True

    class _Backend:
        kernel_observer = _Supported()

    monkeypatch.setattr(kp, "get_arch", lambda: _Backend())

    calls = {"n": 0}

    async def _failing_cycle(observer):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError()
        raise RuntimeError("probe died")

    monkeypatch.setattr(kp, "_run_probe_cycle", _failing_cycle)

    with pytest.raises(asyncio.CancelledError):
        await kp.kernel_perception_loop()

    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# _run_probe_cycle — processes a fixture JSONL stream via mocked subprocess
# ---------------------------------------------------------------------------

class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for line in self._lines:
            yield line


class _FakeProc:
    def __init__(self, lines: list[bytes]) -> None:
        self.stdout = _FakeStdout(lines)
        self.returncode = 0

    def terminate(self) -> None:
        pass

    async def wait(self):
        return 0


class _FakeObserver:
    supports_kernel_observation = True

    def probe_command(self) -> list[str]:
        return ["bpftrace", "-f", "json", "-e", "..."]

    def parse_event(self, line: str):
        import json
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None


@pytest.mark.asyncio
async def test_run_probe_cycle_dispatches_parsed_events(monkeypatch):
    kp._debounce_cache.clear()
    monkeypatch.setattr(kp.settings, "ebpf_perception_comm_denylist", ())
    monkeypatch.setattr(kp.settings, "ebpf_perception_debounce_s", 1000.0)

    import json
    lines = [
        (json.dumps({"event_type": "execve", "comm": "bash", "pid": 1, "path": "/bin/ls"}) + "\n").encode(),
        b"not json {{\n",
    ]
    fake_proc = _FakeProc(lines)

    async def _fake_create_subprocess_exec(*args, **kwargs):
        return fake_proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    recorded = []
    monkeypatch.setattr(kp, "_record_event", lambda *a, **kw: recorded.append((a, kw)))

    await kp._run_probe_cycle(_FakeObserver())

    assert len(recorded) == 1
    (_, event_type, _, metadata), _ = recorded[0]
    assert event_type == kp.EVENT_TYPE_KERNEL_EXEC
    assert metadata["source_type"] == "kernel"
