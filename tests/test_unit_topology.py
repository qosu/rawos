"""tests/test_unit_topology.py — TDD for rawos/kernel/unit_topology.py (Phase 23-full).

TDD Iron Law: this file must go RED before unit_topology.py is written
(ModuleNotFoundError: No module named 'rawos.kernel.unit_topology').

Phase 23-full — Unit/Boot Topology Authorship. The being authors systemd unit
files, enable/disable state (boot symlink graph), and default.target — the final
stack inversion: being owns ALL POLICY, systemd remains the engine/PID1 mechanism.

Two-fact decoupling (mirror 24B):
  Fact A — unit file on disk (inert alone; 0 effect without daemon-reload + start/enable).
  Fact B-runtime — daemon-reload + start/restart (reversible in-band, no reboot, I-UT2).
  Fact B-boot — enable/disable/set-default (manifests ONLY at next boot, reboot-class, I-UT2).

These tests exercise PURE PYTHON LOGIC ONLY — no real systemctl calls, no unit files
written to disk (mocked UnitTopologyManager), no daemon-reload, no reboot. Tests
requiring a live manager or real boot-graph changes are deferred to maintenance
window gates (23F.1+, human-gated, never run by CI against prod).
"""
from __future__ import annotations

import hashlib
import time

import pytest

from rawos.kernel import unit_topology
from rawos.kernel.track_record import GRADUATION_THRESHOLD
from rawos.models import User


# ---------------------------------------------------------------------------
# Minimal mock UnitTopologyManager — no real systemctl, purely in-memory
# ---------------------------------------------------------------------------

class FakeUnitTopologyManager:
    """Mock UnitTopologyManager for unit tests — no real systemctl calls."""

    def __init__(
        self,
        *,
        initial_content: "dict[str, str | None] | None" = None,
        initial_enabled: "dict[str, bool] | None" = None,
        initial_default: str = "multi-user.target",
        analyze_result: "tuple[bool, str]" = (True, ""),
        is_active_result: bool = True,
    ) -> None:
        self._units: "dict[str, str | None]" = dict(initial_content or {})
        self._enabled: "dict[str, bool]" = dict(initial_enabled or {})
        self._default = initial_default
        self._analyze_result = analyze_result
        self._is_active_result = is_active_result
        self.calls: "list[tuple[str, object]]" = []

    def author_unit(self, unit_name: str, content: str) -> None:
        self.calls.append(("author_unit", unit_name))
        self._units[unit_name] = content

    def delete_unit(self, unit_name: str) -> None:
        self.calls.append(("delete_unit", unit_name))
        self._units.pop(unit_name, None)

    def read_unit(self, unit_name: str) -> "str | None":
        return self._units.get(unit_name)

    def enable(self, unit_name: str) -> None:
        self.calls.append(("enable", unit_name))
        self._enabled[unit_name] = True

    def disable(self, unit_name: str) -> None:
        self.calls.append(("disable", unit_name))
        self._enabled[unit_name] = False

    def is_enabled(self, unit_name: str) -> bool:
        return self._enabled.get(unit_name, False)

    def set_default(self, target: str) -> None:
        self.calls.append(("set_default", target))
        self._default = target

    def get_default(self) -> str:
        return self._default

    def daemon_reload(self) -> None:
        self.calls.append(("daemon_reload", None))

    def analyze_verify(self) -> "tuple[bool, str]":
        return self._analyze_result

    def is_active(self, unit_name: str) -> bool:
        return self._is_active_result

    def is_system_running(self) -> bool:
        return True

    def list_dependencies(self, *unit_names: str) -> str:
        return "\n".join(unit_names)


def _fake_mgr(**kwargs: object) -> FakeUnitTopologyManager:
    return FakeUnitTopologyManager(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Shared floor (computed from seed only — no fake deps output)
# ---------------------------------------------------------------------------
_FLOOR = unit_topology.compute_floor_closure("")


# ---------------------------------------------------------------------------
# compute_floor_closure — from list-dependencies output
# ---------------------------------------------------------------------------

_FAKE_DEPS_OUTPUT = """\
sshd.service
● system.slice
  ├─systemd-journald.service
  ├─dbus.service
  └─network.target
     └─systemd-networkd.service
"""


def test_compute_floor_closure_includes_seed():
    closure = unit_topology.compute_floor_closure("")
    for unit in unit_topology.FLOOR_UNIT_SEED:
        assert unit in closure, f"Seed unit {unit!r} missing from closure"


def test_compute_floor_closure_parses_deps_output():
    closure = unit_topology.compute_floor_closure(_FAKE_DEPS_OUTPUT)
    assert "sshd.service" in closure
    assert "dbus.service" in closure
    assert "network.target" in closure
    assert "systemd-networkd.service" in closure


def test_compute_floor_closure_returns_frozenset():
    assert isinstance(unit_topology.compute_floor_closure(""), frozenset)


def test_compute_floor_closure_normalizes_bare_service_names():
    closure = unit_topology.compute_floor_closure("")
    # FLOOR_UNIT_SEED contains "rawos.service" → both forms must be present.
    assert "rawos.service" in closure
    assert "rawos" in closure


# ---------------------------------------------------------------------------
# is_floor_protected
# ---------------------------------------------------------------------------

def test_is_floor_protected_true_for_sshd_service():
    assert unit_topology.is_floor_protected("sshd.service", _FLOOR) is True


def test_is_floor_protected_true_for_sshd_bare():
    assert unit_topology.is_floor_protected("sshd", _FLOOR) is True


def test_is_floor_protected_true_for_systemd():
    assert unit_topology.is_floor_protected("systemd", _FLOOR) is True


def test_is_floor_protected_true_for_rawos_service():
    assert unit_topology.is_floor_protected("rawos.service", _FLOOR) is True


def test_is_floor_protected_false_for_non_floor():
    assert unit_topology.is_floor_protected("my-test-service.service", _FLOOR) is False
    assert unit_topology.is_floor_protected("curl.service", _FLOOR) is False


# ---------------------------------------------------------------------------
# ReversibleUnitTopologyAction construction — floor guard (I-UT3)
# ---------------------------------------------------------------------------

_UNIT_CONTENT = "[Unit]\nDescription=rawos test service\n\n[Service]\nType=oneshot\nExecStart=/bin/true\n"


def test_construction_refuses_sshd_service_author():
    with pytest.raises(unit_topology.UnitTopologyRefusalError):
        unit_topology.ReversibleUnitTopologyAction(
            _fake_mgr(), "sshd.service", "author", _FLOOR,
            unit_content=_UNIT_CONTENT,
        )


def test_construction_refuses_ssh_bare_enable():
    with pytest.raises(unit_topology.UnitTopologyRefusalError):
        unit_topology.ReversibleUnitTopologyAction(
            _fake_mgr(), "ssh", "enable", _FLOOR,
        )


def test_construction_refuses_rawos_service_disable():
    with pytest.raises(unit_topology.UnitTopologyRefusalError):
        unit_topology.ReversibleUnitTopologyAction(
            _fake_mgr(), "rawos.service", "disable", _FLOOR,
        )


def test_construction_refuses_rawos_bpf_lsm_holder_delete():
    with pytest.raises(unit_topology.UnitTopologyRefusalError):
        unit_topology.ReversibleUnitTopologyAction(
            _fake_mgr(), "rawos-bpf-lsm-holder.service", "delete", _FLOOR,
        )


def test_construction_ok_for_non_floor_unit():
    action = unit_topology.ReversibleUnitTopologyAction(
        _fake_mgr(), "my-test-svc.service", "author", _FLOOR,
        unit_content=_UNIT_CONTENT,
    )
    assert action.unit_name == "my-test-svc.service"
    assert action.op == "author"


def test_construction_rejects_unknown_op():
    with pytest.raises(unit_topology.UnitTopologyError):
        unit_topology.ReversibleUnitTopologyAction(
            _fake_mgr(), "my-test-svc.service", "unknown_op", _FLOOR,
        )


def test_construction_author_requires_content():
    with pytest.raises(unit_topology.UnitTopologyError):
        unit_topology.ReversibleUnitTopologyAction(
            _fake_mgr(), "my-test-svc.service", "author", _FLOOR,
            # unit_content omitted intentionally
        )


def test_construction_author_requires_non_empty_content():
    with pytest.raises(unit_topology.UnitTopologyError):
        unit_topology.ReversibleUnitTopologyAction(
            _fake_mgr(), "my-test-svc.service", "author", _FLOOR,
            unit_content="",
        )


# ---------------------------------------------------------------------------
# I-UT4 — set_default allowlist
# ---------------------------------------------------------------------------

def test_set_default_accepts_multi_user_target():
    action = unit_topology.ReversibleUnitTopologyAction(
        _fake_mgr(), "multi-user.target", "set_default", _FLOOR,
        target_name="multi-user.target",
    )
    assert action.op == "set_default"


def test_set_default_accepts_graphical_target():
    action = unit_topology.ReversibleUnitTopologyAction(
        _fake_mgr(), "graphical.target", "set_default", _FLOOR,
        target_name="graphical.target",
    )
    assert action.op == "set_default"


def test_set_default_refuses_rescue_target():
    with pytest.raises(unit_topology.UnitTopologyRefusalError):
        unit_topology.ReversibleUnitTopologyAction(
            _fake_mgr(), "rescue.target", "set_default", _FLOOR,
            target_name="rescue.target",
        )


def test_set_default_refuses_emergency_target():
    with pytest.raises(unit_topology.UnitTopologyRefusalError):
        unit_topology.ReversibleUnitTopologyAction(
            _fake_mgr(), "emergency.target", "set_default", _FLOOR,
            target_name="emergency.target",
        )


def test_set_default_refuses_custom_unverified_target():
    with pytest.raises(unit_topology.UnitTopologyRefusalError):
        unit_topology.ReversibleUnitTopologyAction(
            _fake_mgr(), "my-custom.target", "set_default", _FLOOR,
            target_name="my-custom.target",
        )


# ---------------------------------------------------------------------------
# capture / apply / verify / restore round-trip (I-UT2, I-UT6)
# ---------------------------------------------------------------------------

def test_capture_records_prior_content():
    mgr = _fake_mgr(
        initial_content={"my-test-svc.service": _UNIT_CONTENT},
        initial_enabled={"my-test-svc.service": False},
        initial_default="multi-user.target",
    )
    action = unit_topology.ReversibleUnitTopologyAction(
        mgr, "my-test-svc.service", "author", _FLOOR,
        unit_content="[Unit]\nDescription=updated\n",
    )
    snap = action.capture()
    assert snap.unit_name == "my-test-svc.service"
    assert snap.prior_content == _UNIT_CONTENT
    assert snap.prior_enabled is False
    assert snap.prior_default_target == "multi-user.target"


def test_capture_records_none_for_nonexistent_unit():
    mgr = _fake_mgr()
    action = unit_topology.ReversibleUnitTopologyAction(
        mgr, "my-new-svc.service", "author", _FLOOR,
        unit_content=_UNIT_CONTENT,
    )
    snap = action.capture()
    assert snap.prior_content is None


def test_apply_author_writes_unit_and_daemon_reloads():
    mgr = _fake_mgr()
    action = unit_topology.ReversibleUnitTopologyAction(
        mgr, "my-test-svc.service", "author", _FLOOR,
        unit_content=_UNIT_CONTENT,
    )
    action.apply()
    assert mgr.read_unit("my-test-svc.service") == _UNIT_CONTENT
    assert ("daemon_reload", None) in mgr.calls


def test_apply_delete_removes_unit_and_daemon_reloads():
    mgr = _fake_mgr(initial_content={"my-test-svc.service": _UNIT_CONTENT})
    action = unit_topology.ReversibleUnitTopologyAction(
        mgr, "my-test-svc.service", "delete", _FLOOR,
    )
    action.apply()
    assert mgr.read_unit("my-test-svc.service") is None
    assert ("daemon_reload", None) in mgr.calls


def test_apply_enable_enables_unit():
    mgr = _fake_mgr()
    action = unit_topology.ReversibleUnitTopologyAction(
        mgr, "my-test-svc.service", "enable", _FLOOR,
    )
    action.apply()
    assert ("enable", "my-test-svc.service") in mgr.calls


def test_apply_disable_disables_unit():
    mgr = _fake_mgr(initial_enabled={"my-test-svc.service": True})
    action = unit_topology.ReversibleUnitTopologyAction(
        mgr, "my-test-svc.service", "disable", _FLOOR,
    )
    action.apply()
    assert ("disable", "my-test-svc.service") in mgr.calls


def test_apply_set_default_changes_target():
    mgr = _fake_mgr(initial_default="multi-user.target")
    action = unit_topology.ReversibleUnitTopologyAction(
        mgr, "graphical.target", "set_default", _FLOOR,
        target_name="graphical.target",
    )
    action.apply()
    assert mgr.get_default() == "graphical.target"


def test_verify_returns_true_on_analyze_ok():
    mgr = _fake_mgr(analyze_result=(True, ""))
    action = unit_topology.ReversibleUnitTopologyAction(
        mgr, "my-test-svc.service", "author", _FLOOR,
        unit_content=_UNIT_CONTENT,
    )
    assert action.verify() is True


def test_verify_returns_false_on_analyze_fail():
    mgr = _fake_mgr(analyze_result=(False, "ordering cycle detected"))
    action = unit_topology.ReversibleUnitTopologyAction(
        mgr, "my-test-svc.service", "author", _FLOOR,
        unit_content=_UNIT_CONTENT,
    )
    assert action.verify() is False


def test_restore_author_reverts_to_original_content():
    mgr = _fake_mgr(initial_content={"my-test-svc.service": _UNIT_CONTENT})
    new_content = "[Unit]\nDescription=updated\n"
    action = unit_topology.ReversibleUnitTopologyAction(
        mgr, "my-test-svc.service", "author", _FLOOR,
        unit_content=new_content,
    )
    snap = action.capture()
    action.apply()
    assert mgr.read_unit("my-test-svc.service") == new_content  # applied
    action.restore(snap)
    assert mgr.read_unit("my-test-svc.service") == _UNIT_CONTENT  # restored


def test_restore_author_deletes_if_unit_was_nonexistent():
    mgr = _fake_mgr()  # unit does not exist initially
    action = unit_topology.ReversibleUnitTopologyAction(
        mgr, "my-new-svc.service", "author", _FLOOR,
        unit_content=_UNIT_CONTENT,
    )
    snap = action.capture()         # prior_content = None
    assert snap.prior_content is None
    action.apply()                   # creates the unit
    assert mgr.read_unit("my-new-svc.service") == _UNIT_CONTENT
    action.restore(snap)             # must delete (back to nonexistent)
    assert mgr.read_unit("my-new-svc.service") is None


def test_restore_enable_reverts_to_disabled():
    mgr = _fake_mgr(initial_enabled={"my-test-svc.service": False})
    action = unit_topology.ReversibleUnitTopologyAction(
        mgr, "my-test-svc.service", "enable", _FLOOR,
    )
    snap = action.capture()
    action.apply()
    assert mgr.is_enabled("my-test-svc.service") is True
    action.restore(snap)
    assert mgr.is_enabled("my-test-svc.service") is False


def test_restore_set_default_reverts_prior_target():
    mgr = _fake_mgr(initial_default="multi-user.target")
    action = unit_topology.ReversibleUnitTopologyAction(
        mgr, "graphical.target", "set_default", _FLOOR,
        target_name="graphical.target",
    )
    snap = action.capture()
    action.apply()
    assert mgr.get_default() == "graphical.target"
    action.restore(snap)
    assert mgr.get_default() == "multi-user.target"


# ---------------------------------------------------------------------------
# I-UT2 / I-UT6 — boot-graph ops do NOT daemon-reload on apply
# ---------------------------------------------------------------------------

def test_boot_graph_enable_does_not_daemon_reload():
    mgr = _fake_mgr()
    action = unit_topology.ReversibleUnitTopologyAction(
        mgr, "my-test-svc.service", "enable", _FLOOR,
    )
    action.apply()
    reload_calls = [c for c in mgr.calls if c[0] == "daemon_reload"]
    assert reload_calls == [], (
        "enable must not daemon-reload on apply (boot-graph op, I-UT2)"
    )


def test_boot_graph_disable_does_not_daemon_reload():
    mgr = _fake_mgr()
    action = unit_topology.ReversibleUnitTopologyAction(
        mgr, "my-test-svc.service", "disable", _FLOOR,
    )
    action.apply()
    reload_calls = [c for c in mgr.calls if c[0] == "daemon_reload"]
    assert reload_calls == [], (
        "disable must not daemon-reload on apply (boot-graph op, I-UT2)"
    )


def test_boot_graph_set_default_does_not_daemon_reload():
    mgr = _fake_mgr()
    action = unit_topology.ReversibleUnitTopologyAction(
        mgr, "graphical.target", "set_default", _FLOOR,
        target_name="graphical.target",
    )
    action.apply()
    reload_calls = [c for c in mgr.calls if c[0] == "daemon_reload"]
    assert reload_calls == [], (
        "set_default must not daemon-reload on apply (boot-graph op, I-UT2)"
    )


# ---------------------------------------------------------------------------
# validate_boot_config — I-UT9 fail-fast (monkeypatch like test_bpf_lsm.py)
# ---------------------------------------------------------------------------

def test_validate_boot_config_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(unit_topology, "_support_cache", False)
    unit_topology.validate_boot_config(enabled=False)  # must not raise


def test_validate_boot_config_raises_when_enabled_and_unsupported(monkeypatch):
    monkeypatch.setattr(unit_topology, "_support_cache", False)
    with pytest.raises(unit_topology.UnitTopologyUnsupportedError):
        unit_topology.validate_boot_config(enabled=True)


def test_validate_boot_config_ok_when_enabled_and_supported(monkeypatch):
    monkeypatch.setattr(unit_topology, "_support_cache", True)
    monkeypatch.setattr(unit_topology, "_get_default_target", lambda: "multi-user.target")
    unit_topology.validate_boot_config(enabled=True)  # must not raise


def test_validate_boot_config_raises_when_default_target_disallowed(monkeypatch):
    monkeypatch.setattr(unit_topology, "_support_cache", True)
    monkeypatch.setattr(unit_topology, "_get_default_target", lambda: "rescue.target")
    with pytest.raises(unit_topology.UnitTopologyError):
        unit_topology.validate_boot_config(enabled=True)


def test_validate_boot_config_disabled_is_noop_regardless_of_support(monkeypatch):
    # Even unsupported + disallowed target: disabled = no-op (I-UT11).
    monkeypatch.setattr(unit_topology, "_support_cache", False)
    monkeypatch.setattr(unit_topology, "_get_default_target", lambda: "rescue.target")
    unit_topology.validate_boot_config(enabled=False)  # must not raise


# ---------------------------------------------------------------------------
# Boot-deadman arm / disarm — pure functions (I-UT8)
# ---------------------------------------------------------------------------

def test_arm_boot_deadman_emits_systemd_run():
    cmd = unit_topology.arm_boot_deadman(delay_s=300, revert_cmd="systemctl reboot")
    assert cmd[0] == "systemd-run"


def test_arm_boot_deadman_includes_on_active():
    cmd = unit_topology.arm_boot_deadman(delay_s=300, revert_cmd="systemctl reboot")
    assert "--on-active" in cmd


def test_arm_boot_deadman_delay_follows_on_active():
    cmd = unit_topology.arm_boot_deadman(delay_s=300, revert_cmd="systemctl reboot")
    idx = cmd.index("--on-active")
    assert cmd[idx + 1] == "300"


def test_arm_boot_deadman_includes_unit_name():
    cmd = unit_topology.arm_boot_deadman(delay_s=300, revert_cmd="systemctl reboot")
    assert any("rawos-unit-topology-revert" in p for p in cmd)


def test_arm_boot_deadman_custom_delay():
    cmd = unit_topology.arm_boot_deadman(delay_s=600, revert_cmd="systemctl reboot")
    idx = cmd.index("--on-active")
    assert cmd[idx + 1] == "600"


def test_disarm_boot_deadman_emits_systemctl_stop():
    cmd = unit_topology.disarm_boot_deadman()
    assert cmd[0] == "systemctl"
    assert "stop" in cmd


def test_disarm_boot_deadman_references_revert_unit():
    cmd = unit_topology.disarm_boot_deadman()
    assert "rawos-unit-topology-revert" in cmd


def test_arm_and_disarm_reference_same_unit():
    arm = unit_topology.arm_boot_deadman(delay_s=300, revert_cmd="systemctl reboot")
    disarm = unit_topology.disarm_boot_deadman()
    unit_in_arm = next(
        (p.split("=", 1)[1] for p in arm if p.startswith("--unit=")), None
    )
    assert unit_in_arm is not None
    assert unit_in_arm in disarm


# ---------------------------------------------------------------------------
# operate_on_unit_topology gate (I-UT7)
# ---------------------------------------------------------------------------

import rawos.db as db
from rawos.kernel.operator import (
    UnitTopologyRefusalError as OperatorUnitTopologyRefusalError,
    operate_on_unit_topology,
)


class TestOperateOnUnitTopologyGate:
    @pytest.fixture(autouse=True)
    def setup_db(self, tmp_path):
        db.init(str(tmp_path / "test.db"))
        self.user = db.create_user(User(
            email=f"ut-gate-{id(self)}@test.com",
            password_hash=hashlib.sha256(b"pass").hexdigest(),
        ))
        yield

    def test_propose_only_when_disabled(self, monkeypatch):
        monkeypatch.setattr("rawos.config.settings.operator_unit_topology_enabled", False)
        outcome = operate_on_unit_topology(
            self.user.id, "my-test-svc.service", "author",
            unit_topology.compute_floor_closure(""),
            mgr=_fake_mgr(),
            unit_content=_UNIT_CONTENT,
        )
        assert outcome.auto_applied is False
        assert outcome.proposed is True
        assert "operator_unit_topology_enabled=False" in outcome.reason

    def test_propose_only_when_not_in_allowlist(self, monkeypatch):
        monkeypatch.setattr("rawos.config.settings.operator_unit_topology_enabled", True)
        # No db.add_managed_unit_target → not in allowlist.
        outcome = operate_on_unit_topology(
            self.user.id, "my-test-svc.service", "author",
            unit_topology.compute_floor_closure(""),
            mgr=_fake_mgr(),
            unit_content=_UNIT_CONTENT,
        )
        assert outcome.auto_applied is False
        assert "managed_unit_targets" in outcome.reason

    def test_boot_graph_op_always_propose_only(self, monkeypatch):
        """I-UT7: boot-graph ops NEVER auto-apply regardless of enabled/graduation."""
        monkeypatch.setattr("rawos.config.settings.operator_unit_topology_enabled", True)
        db.add_managed_unit_target(self.user.id, "my-test-svc.service")
        # Graduate enable op: GRADUATION_THRESHOLD*2 calls required (stability window).
        _now = 1_700_000_000
        for i in range(GRADUATION_THRESHOLD * 2):
            db.update_operator_track_record(
                self.user.id, "unit_topology_enable", "my-test-svc.service",
                verified=True, now=_now + i,
            )
        outcome = operate_on_unit_topology(
            self.user.id, "my-test-svc.service", "enable",
            unit_topology.compute_floor_closure(""),
            mgr=_fake_mgr(),
        )
        assert outcome.auto_applied is False
        assert outcome.proposed is True
        reason_lower = outcome.reason.lower()
        assert "boot-graph" in reason_lower or "propose-only" in reason_lower

    def test_floor_refusal_propagates_unconditionally(self, monkeypatch):
        """UnitTopologyRefusalError for floor units propagates even before allowlist check."""
        monkeypatch.setattr("rawos.config.settings.operator_unit_topology_enabled", True)
        with pytest.raises(OperatorUnitTopologyRefusalError):
            operate_on_unit_topology(
                self.user.id, "sshd.service", "author",
                unit_topology.compute_floor_closure(""),
                mgr=_fake_mgr(),
                unit_content=_UNIT_CONTENT,
            )

    def test_propose_only_when_not_graduated(self, monkeypatch):
        monkeypatch.setattr("rawos.config.settings.operator_unit_topology_enabled", True)
        db.add_managed_unit_target(self.user.id, "my-test-svc.service")
        # No track record → not graduated.
        outcome = operate_on_unit_topology(
            self.user.id, "my-test-svc.service", "author",
            unit_topology.compute_floor_closure(""),
            mgr=_fake_mgr(),
            unit_content=_UNIT_CONTENT,
        )
        assert outcome.auto_applied is False
        assert "graduated" in outcome.reason.lower()


# ---------------------------------------------------------------------------
# Flag-off no-op — I-UT11
# ---------------------------------------------------------------------------

def test_flag_off_does_not_invoke_systemctl(monkeypatch):
    """operator_unit_topology_enabled=False → no manager calls at all."""
    monkeypatch.setattr("rawos.config.settings.operator_unit_topology_enabled", False)
    mgr = _fake_mgr()
    operate_on_unit_topology(
        "fake-user-id", "my-test-svc.service", "author",
        unit_topology.compute_floor_closure(""),
        mgr=mgr,
        unit_content=_UNIT_CONTENT,
    )
    assert mgr.calls == [], (
        "No manager calls should be made when operator_unit_topology_enabled=False"
    )
