"""tests/test_capability_gate.py — SHP.4 I-SEC6 unified capability gate tests.

Verifies tier classification and audit-first gate policy.
SHP.4: all tiers allowed, all tool calls audited (I-SEC11 audit-first).
SHP.6: TIER3 enforcement flip will be tested there.
"""
from __future__ import annotations


import pytest
from rawos.kernel.capability_gate import (
    CapabilityTier,
    classify_tier,
    pre_execute_gate,
)


class TestCapabilityTierClassification:
    """Tier classification must correctly identify privilege level of each tool call."""

    def test_read_file_is_tier0(self):
        assert classify_tier("read_file", {}) == CapabilityTier.TIER0

    def test_list_files_is_tier0(self):
        assert classify_tier("list_files", {}) == CapabilityTier.TIER0

    def test_bash_readonly_is_tier0(self):
        assert classify_tier("bash_readonly", {}) == CapabilityTier.TIER0

    def test_bash_is_tier1(self):
        assert classify_tier("bash", {}) == CapabilityTier.TIER1

    def test_write_file_is_tier1(self):
        assert classify_tier("write_file", {}) == CapabilityTier.TIER1

    def test_manage_file_is_tier2(self):
        assert classify_tier("manage_file", {"action": "edit"}) == CapabilityTier.TIER2

    def test_manage_service_status_is_tier2(self):
        assert classify_tier("manage_service", {"action": "status"}) == CapabilityTier.TIER2

    def test_manage_service_restart_is_tier3(self):
        params = {"action": "action", "svc_action": "restart"}
        assert classify_tier("manage_service", params) == CapabilityTier.TIER3

    def test_manage_service_stop_is_tier3(self):
        params = {"action": "action", "svc_action": "stop"}
        assert classify_tier("manage_service", params) == CapabilityTier.TIER3

    def test_manage_pam_is_tier3(self):
        assert classify_tier("manage_pam", {}) == CapabilityTier.TIER3

    def test_unknown_tool_defaults_to_tier0(self):
        """Unknown tools fall through to TIER0 (execute() blocks them as unregistered)."""
        assert classify_tier("nonexistent_tool", {}) == CapabilityTier.TIER0


class TestGatePolicy:
    """SHP.4 audit-first policy: all tiers allowed, all calls logged (I-SEC11)."""

    def test_tier0_allowed(self):
        result = pre_execute_gate("read_file", {}, "/workspace", "u1")
        assert result.allowed is True
        assert result.tier == CapabilityTier.TIER0

    def test_tier1_allowed(self):
        result = pre_execute_gate("bash", {}, "/workspace", "u1")
        assert result.allowed is True
        assert result.tier == CapabilityTier.TIER1

    def test_tier2_allowed(self):
        result = pre_execute_gate("manage_file", {"action": "edit"}, "/workspace", "u1")
        assert result.allowed is True
        assert result.tier == CapabilityTier.TIER2

    def test_tier3_allowed_audit_first(self):
        """SHP.4: TIER3 allowed but logged at WARNING; enforcement deferred to SHP.6."""
        params = {"action": "action", "svc_action": "restart"}
        result = pre_execute_gate("manage_service", params, "/workspace", "u1")
        assert result.allowed is True
        assert result.tier == CapabilityTier.TIER3


class TestGateAudit:
    """Every tool call must generate a structured audit record in the chain."""

    def test_audit_record_written_for_tool_call(self, tmp_path, monkeypatch):
        import rawos.kernel.audit_chain as ac
        from rawos.kernel.audit_chain import _AuditChain

        chain_inst = _AuditChain(
            chain_path=tmp_path / "chain.jsonl",
            key_path=tmp_path / "key.pem",
            pub_path=tmp_path / "pub.pem",
        )
        monkeypatch.setattr(ac, "_chain", chain_inst)

        pre_execute_gate("bash", {"command": "echo hi"}, "/workspace/proj", "u1")

        records = chain_inst.read_all()
        assert len(records) == 1
        payload = records[0]["payload"]
        assert payload["tool"] == "bash"
        assert payload["tier"] == CapabilityTier.TIER1.value
        assert payload["allowed"] is True
        assert "workdir" in payload

    def test_audit_records_tier3_call(self, tmp_path, monkeypatch):
        """TIER3 audit record must be present with tier=3."""
        import rawos.kernel.audit_chain as ac
        from rawos.kernel.audit_chain import _AuditChain

        chain_inst = _AuditChain(
            chain_path=tmp_path / "chain.jsonl",
            key_path=tmp_path / "key.pem",
            pub_path=tmp_path / "pub.pem",
        )
        monkeypatch.setattr(ac, "_chain", chain_inst)

        pre_execute_gate("manage_pam", {}, "/workspace", "u1")

        records = chain_inst.read_all()
        payload = records[0]["payload"]
        assert payload["tier"] == CapabilityTier.TIER3.value

    def test_audit_failure_does_not_propagate(self, monkeypatch):
        """Audit chain failure must never block tool execution."""
        import rawos.kernel.audit_chain as ac

        def _fail(*args, **kwargs):
            raise IOError("disk full")

        monkeypatch.setattr(ac, "append", _fail)

        result = pre_execute_gate("bash", {}, "/workspace", "u1")
        assert result.allowed is True
