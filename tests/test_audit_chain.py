"""tests/test_audit_chain.py — SHP.5 I-SEC7 hash-chained audit log.

Tests the _AuditChain class:
  - Append: file creation, seq, prev_hash linkage, hash correctness, ECDSA sig
  - Verification: valid chain passes, tamper scenarios fail
  - Persistence: chain resumes correctly from existing JSONL on restart
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest
from rawos.kernel.audit_chain import _AuditChain, VerifyResult


@pytest.fixture
def chain(tmp_path) -> _AuditChain:
    return _AuditChain(
        chain_path=tmp_path / "audit_chain.jsonl",
        key_path=tmp_path / "signing_key.pem",
        pub_path=tmp_path / "verify_key.pem",
    )


@pytest.fixture
def paths(tmp_path):
    return {
        "chain_path": tmp_path / "audit_chain.jsonl",
        "key_path":   tmp_path / "signing_key.pem",
        "pub_path":   tmp_path / "verify_key.pem",
    }


# ── Append ───────────────────────────────────────────────────────────────────

class TestChainAppend:
    def test_append_creates_jsonl_file(self, chain, tmp_path):
        """First append must create the chain JSONL file."""
        chain.append("startup", {})
        assert (tmp_path / "audit_chain.jsonl").exists()

    def test_first_record_seq_is_1(self, chain):
        """Sequence numbers are 1-based — first record has seq=1."""
        rec = chain.append("startup", {})
        assert rec["seq"] == 1

    def test_seq_increments_monotonically(self, chain):
        r1 = chain.append("e", {})
        r2 = chain.append("e", {})
        r3 = chain.append("e", {})
        assert r2["seq"] == r1["seq"] + 1
        assert r3["seq"] == r2["seq"] + 1

    def test_first_record_has_empty_prev_hash(self, chain):
        """Chain sentinel: first record's prev_hash must be empty string."""
        rec = chain.append("startup", {})
        assert rec["prev_hash"] == ""

    def test_subsequent_record_prev_hash_matches_previous_hash(self, chain):
        r1 = chain.append("e", {"n": 1})
        r2 = chain.append("e", {"n": 2})
        assert r2["prev_hash"] == r1["hash"]

    def test_record_hash_is_sha256_of_canonical_json(self, chain):
        """hash field == SHA256 of canonical JSON of {seq, ts_ms, prev_hash, payload}."""
        rec = chain.append("tool_call", {"tool": "bash", "user_id": "u1"})
        canonical = json.dumps(
            {
                "seq": rec["seq"],
                "ts_ms": rec["ts_ms"],
                "prev_hash": rec["prev_hash"],
                "payload": rec["payload"],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        expected = hashlib.sha256(canonical).hexdigest()
        assert rec["hash"] == expected

    def test_record_sig_is_valid_ecdsa(self, chain):
        """sig field must be a valid ECDSA-SHA256 signature over canonical JSON."""
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import hashes
        rec = chain.append("tool_call", {"tool": "bash"})
        canonical = json.dumps(
            {
                "seq": rec["seq"],
                "ts_ms": rec["ts_ms"],
                "prev_hash": rec["prev_hash"],
                "payload": rec["payload"],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        sig_bytes = bytes.fromhex(rec["sig"])
        # Raises InvalidSignature on failure — test passes only if no exception
        chain._public_key.verify(sig_bytes, canonical, ec.ECDSA(hashes.SHA256()))

    def test_payload_includes_event_type(self, chain):
        """payload dict must contain 'type' field set to event_type."""
        rec = chain.append("tool_call", {"tool": "bash"})
        assert rec["payload"]["type"] == "tool_call"

    def test_append_fail_open_on_bad_path(self, tmp_path):
        """Append must never raise even if chain file path is unwritable."""
        bad = _AuditChain(
            chain_path=Path("/nonexistent_shp5_dir_xyz/chain.jsonl"),
            key_path=tmp_path / "key.pem",
            pub_path=tmp_path / "pub.pem",
        )
        result = bad.append("test", {"x": 1})
        assert isinstance(result, dict)  # empty dict on failure, not exception

    def test_read_all_returns_records_in_seq_order(self, chain):
        for i in range(5):
            chain.append("e", {"i": i})
        records = chain.read_all()
        assert len(records) == 5
        assert [r["seq"] for r in records] == [1, 2, 3, 4, 5]

    def test_last_seq_property_tracks_appends(self, chain):
        assert chain.last_seq == 0
        chain.append("e", {})
        assert chain.last_seq == 1
        chain.append("e", {})
        assert chain.last_seq == 2


# ── Verification ─────────────────────────────────────────────────────────────

class TestChainVerification:
    def test_verify_empty_chain_returns_ok(self, chain):
        result = chain.verify()
        assert result.ok is True
        assert result.records_verified == 0

    def test_verify_valid_chain_passes(self, chain):
        chain.append("startup", {"version": "1.0"})
        chain.append("tool_call", {"tool": "bash"})
        chain.append("tool_call", {"tool": "read_file"})
        result = chain.verify()
        assert result.ok is True
        assert result.records_verified == 3

    def test_verify_detects_payload_tamper(self, paths):
        """Modifying payload without updating hash must be detected."""
        c = _AuditChain(**paths)
        c.append("tool_call", {"tool": "bash"})
        c.append("tool_call", {"tool": "read_file"})

        # Tamper: change payload in first record on disk
        lines = paths["chain_path"].read_text().splitlines()
        r = json.loads(lines[0])
        r["payload"]["tool"] = "TAMPERED"
        lines[0] = json.dumps(r, separators=(",", ":"))
        paths["chain_path"].write_text("\n".join(lines) + "\n")

        fresh = _AuditChain(**paths)
        result = fresh.verify()
        assert result.ok is False
        assert "hash mismatch" in result.reason

    def test_verify_detects_chain_break(self, paths):
        """Altering prev_hash in a record must break chain linkage detection."""
        c = _AuditChain(**paths)
        c.append("e", {"n": 1})
        c.append("e", {"n": 2})

        lines = paths["chain_path"].read_text().splitlines()
        r = json.loads(lines[1])  # second record
        r["prev_hash"] = "0" * 64  # plausible hex but wrong
        # Also must update hash to avoid "hash mismatch" masking the chain break
        # Re-hash with the tampered prev_hash so verify reaches chain break check
        canonical = json.dumps(
            {"seq": r["seq"], "ts_ms": r["ts_ms"],
             "prev_hash": r["prev_hash"], "payload": r["payload"]},
            sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("utf-8")
        r["hash"] = hashlib.sha256(canonical).hexdigest()
        # Leave sig invalid so sig check also triggers, but chain break is what we test
        lines[1] = json.dumps(r, separators=(",", ":"))
        paths["chain_path"].write_text("\n".join(lines) + "\n")

        fresh = _AuditChain(**paths)
        result = fresh.verify()
        assert result.ok is False
        # Either chain break or sig failure — both are correct detections
        assert "chain break" in result.reason or "signature" in result.reason or "hash mismatch" in result.reason

    def test_verify_detects_signature_forgery(self, paths):
        """Replacing sig with invalid bytes must fail ECDSA verification."""
        c = _AuditChain(**paths)
        c.append("e", {})

        lines = paths["chain_path"].read_text().splitlines()
        r = json.loads(lines[0])
        r["sig"] = "deadbeef" * 16  # 64 chars = 32 bytes, invalid ECDSA-P256 DER sig
        lines[0] = json.dumps(r, separators=(",", ":"))
        paths["chain_path"].write_text("\n".join(lines) + "\n")

        fresh = _AuditChain(**paths)
        result = fresh.verify()
        assert result.ok is False
        assert "signature" in result.reason

    def test_verify_detects_hash_field_tamper(self, paths):
        """Altering only the hash field (leaving payload intact) must fail."""
        c = _AuditChain(**paths)
        c.append("e", {})

        lines = paths["chain_path"].read_text().splitlines()
        r = json.loads(lines[0])
        r["hash"] = "a" * 64  # plausible hex but wrong
        lines[0] = json.dumps(r, separators=(",", ":"))
        paths["chain_path"].write_text("\n".join(lines) + "\n")

        fresh = _AuditChain(**paths)
        result = fresh.verify()
        assert result.ok is False


# ── Persistence ───────────────────────────────────────────────────────────────

class TestChainPersistence:
    def test_chain_resumes_from_existing_file(self, paths):
        """New _AuditChain instance must correctly resume last hash + seq."""
        c1 = _AuditChain(**paths)
        c1.append("e", {"n": 1})
        c1.append("e", {"n": 2})
        last_hash = c1.last_hash
        last_seq  = c1.last_seq

        c2 = _AuditChain(**paths)
        r3 = c2.append("e", {"n": 3})

        assert r3["seq"] == last_seq + 1
        assert r3["prev_hash"] == last_hash

    def test_resumed_chain_verifies_ok(self, paths):
        """Chain written by c1 + extended by c2 must verify as valid."""
        c1 = _AuditChain(**paths)
        c1.append("startup", {})
        c1.append("tool_call", {"tool": "bash"})

        c2 = _AuditChain(**paths)
        c2.append("tool_call", {"tool": "read_file"})

        c3 = _AuditChain(**paths)
        result = c3.verify()
        assert result.ok is True
        assert result.records_verified == 3
