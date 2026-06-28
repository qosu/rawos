"""rawos/kernel/audit_chain.py — SHP.5 I-SEC7 hash-chained audit log.

Every privileged action + agent decision + startup event is appended to an
append-only JSONL chain where each record carries:
  seq       — monotonically increasing integer (1-based)
  ts_ms     — Unix timestamp milliseconds (integer; deterministic JSON)
  prev_hash — SHA-256 hex of previous record's canonical JSON ("" for first)
  payload   — event-specific dict, includes 'type' field
  hash      — SHA-256 hex of canonical JSON of {seq, ts_ms, prev_hash, payload}
  sig       — ECDSA-SHA256 signature (DER, hex) over canonical JSON bytes

Verification checks:
  1. hash == SHA256(canonical)                      — content integrity
  2. ECDSA_verify(sig, canonical, public_key)       — authenticity
  3. record.prev_hash == previous_record.hash       — chain linkage
  4. record.seq == previous_record.seq + 1          — monotonicity

Residual risk (stated, not hidden): signing key lives on-box. A root attacker
who compromises the box can re-sign a rewritten chain. The off-box Telegram
mirror is the primary tamper-evidence anchor — divergence from the mirrored
chain-head is detectable even after host compromise. This is detective, not
preventive (Q1 decision, I-SEC1 zero-lockout floor preserved).
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import socket
import threading
import time
from pathlib import Path

log = logging.getLogger("rawos.audit_chain")

_DEFAULT_CHAIN_PATH = Path("/root/rawos/data/audit_chain.jsonl")
_DEFAULT_KEY_PATH   = Path("/root/rawos/data/audit_signing_key.pem")
_DEFAULT_PUB_PATH   = Path("/root/rawos/data/audit_verify_key.pem")

MIRROR_INTERVAL_S = 1800  # 30 min between Telegram mirror pushes


# ── ECDSA P-256 helpers ───────────────────────────────────────────────────────

def _load_private_key(pem_bytes: bytes):
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    return load_pem_private_key(pem_bytes, password=None)


def _generate_key():
    from cryptography.hazmat.primitives.asymmetric import ec
    return ec.generate_private_key(ec.SECP256R1())


def _sign(private_key, data: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes
    return private_key.sign(data, ec.ECDSA(hashes.SHA256()))


def _verify_sig(public_key, data: bytes, signature: bytes) -> bool:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import hashes
    try:
        public_key.verify(signature, data, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


def _key_to_pem_private(key) -> bytes:
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, NoEncryption,
    )
    return key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


def _key_to_pem_public(key) -> bytes:
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat,
    )
    return key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)


# ── Canonical JSON ────────────────────────────────────────────────────────────

def _canonical(seq: int, ts_ms: int, prev_hash: str, payload: dict) -> bytes:
    """Deterministic canonical JSON bytes for hashing and signing."""
    return json.dumps(
        {"seq": seq, "ts_ms": ts_ms, "prev_hash": prev_hash, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── Verification result ───────────────────────────────────────────────────────

@dataclasses.dataclass(frozen=True)
class VerifyResult:
    ok: bool
    records_verified: int
    reason: str = ""


# ── _AuditChain ───────────────────────────────────────────────────────────────

class _AuditChain:
    """Append-only hash-chained audit log with ECDSA-P256 signing.

    Thread-safe: all mutable state protected by _lock.
    Instances can share the same key file (they re-load the key on init).
    """

    def __init__(
        self,
        chain_path: Path = _DEFAULT_CHAIN_PATH,
        key_path:   Path = _DEFAULT_KEY_PATH,
        pub_path:   Path = _DEFAULT_PUB_PATH,
    ) -> None:
        self._chain_path = chain_path
        self._key_path   = key_path
        self._pub_path   = pub_path
        self._lock       = threading.Lock()
        self._private_key = None
        self._public_key  = None
        self._last_hash   = ""   # "" = chain empty
        self._seq         = 0    # 0 = no records yet; next append gets seq=1
        self._last_ts_ms  = 0

        self._init()

    # ── init ─────────────────────────────────────────────────────────────────

    def _init(self) -> None:
        self._chain_path.parent.mkdir(parents=True, exist_ok=True)
        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        self._private_key, self._public_key = self._load_or_create_keys()
        self._load_chain_tail()

    def _load_or_create_keys(self):
        if self._key_path.exists():
            try:
                priv = _load_private_key(self._key_path.read_bytes())
                return priv, priv.public_key()
            except Exception as exc:
                log.error(
                    "audit_chain: failed to load signing key at %s, regenerating: %s",
                    self._key_path, exc,
                )

        priv    = _generate_key()
        priv_pem = _key_to_pem_private(priv)
        pub_pem  = _key_to_pem_public(priv)

        self._key_path.write_bytes(priv_pem)
        self._key_path.chmod(0o600)
        if self._pub_path:
            self._pub_path.write_bytes(pub_pem)
            self._pub_path.chmod(0o644)

        log.info(
            "audit_chain: new ECDSA P-256 key pair created at %s + %s",
            self._key_path, self._pub_path,
        )
        return priv, priv.public_key()

    def _load_chain_tail(self) -> None:
        """Scan existing chain file for last record to restore seq + last_hash."""
        if not self._chain_path.exists():
            return
        last_line = b""
        try:
            with self._chain_path.open("rb") as fh:
                for line in fh:
                    stripped = line.strip()
                    if stripped:
                        last_line = stripped
        except Exception as exc:
            log.warning("audit_chain: failed reading chain tail: %s", exc)
            return

        if not last_line:
            return
        try:
            rec = json.loads(last_line)
            self._last_hash  = rec["hash"]
            self._seq        = rec["seq"]
            self._last_ts_ms = rec["ts_ms"]
        except Exception as exc:
            log.warning("audit_chain: failed parsing chain tail record: %s", exc)

    # ── append ────────────────────────────────────────────────────────────────

    def append(self, event_type: str, payload: dict) -> dict:
        """Append signed record to chain. Thread-safe. Returns record dict.

        On any failure returns empty dict — never raises.
        """
        try:
            return self._append_locked(event_type, payload)
        except Exception as exc:
            log.error("audit_chain: append failed: %s", exc)
            return {}

    def _append_locked(self, event_type: str, payload: dict) -> dict:
        with self._lock:
            full_payload = {"type": event_type, **payload}
            seq       = self._seq + 1
            ts_ms     = int(time.time() * 1000)
            prev_hash = self._last_hash

            can      = _canonical(seq, ts_ms, prev_hash, full_payload)
            hash_hex = _hash_bytes(can)
            sig_hex  = _sign(self._private_key, can).hex()

            record = {
                "seq":       seq,
                "ts_ms":     ts_ms,
                "prev_hash": prev_hash,
                "payload":   full_payload,
                "hash":      hash_hex,
                "sig":       sig_hex,
            }

            with self._chain_path.open("a") as fh:
                fh.write(json.dumps(record, separators=(",", ":")) + "\n")

            self._last_hash  = hash_hex
            self._seq        = seq
            self._last_ts_ms = ts_ms

            return record

    # ── read + verify ─────────────────────────────────────────────────────────

    def read_all(self) -> list[dict]:
        """Read all chain records from disk in seq order."""
        if not self._chain_path.exists():
            return []
        records: list[dict] = []
        try:
            with self._chain_path.open() as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
        except Exception as exc:
            log.warning("audit_chain: read_all failed: %s", exc)
        return records

    def verify(self) -> VerifyResult:
        """Verify chain integrity: hash, ECDSA signature, prev_hash linkage, seq.

        Returns VerifyResult(ok=True) for empty chain.
        """
        records = self.read_all()
        if not records:
            return VerifyResult(ok=True, records_verified=0, reason="empty chain")

        prev_hash = ""
        for i, rec in enumerate(records):
            # 1. Rebuild canonical and check stored hash
            try:
                can = _canonical(
                    rec["seq"], rec["ts_ms"],
                    rec["prev_hash"], rec["payload"],
                )
            except KeyError as exc:
                return VerifyResult(
                    ok=False, records_verified=i,
                    reason=f"record {i}: missing field {exc}",
                )

            expected_hash = _hash_bytes(can)
            if rec["hash"] != expected_hash:
                return VerifyResult(
                    ok=False, records_verified=i,
                    reason=f"record {i} (seq={rec['seq']}): hash mismatch",
                )

            # 2. Check prev_hash chain linkage
            if rec["prev_hash"] != prev_hash:
                return VerifyResult(
                    ok=False, records_verified=i,
                    reason=(
                        f"record {i} (seq={rec['seq']}): chain break — "
                        f"prev_hash mismatch"
                    ),
                )

            # 3. Verify ECDSA signature
            try:
                sig_bytes = bytes.fromhex(rec["sig"])
            except (ValueError, KeyError):
                return VerifyResult(
                    ok=False, records_verified=i,
                    reason=f"record {i} (seq={rec['seq']}): invalid sig field",
                )
            if not _verify_sig(self._public_key, can, sig_bytes):
                return VerifyResult(
                    ok=False, records_verified=i,
                    reason=f"record {i} (seq={rec['seq']}): signature invalid",
                )

            # 4. Seq monotonicity
            if i > 0 and rec["seq"] != records[i - 1]["seq"] + 1:
                return VerifyResult(
                    ok=False, records_verified=i,
                    reason=(
                        f"record {i}: seq gap — expected "
                        f"{records[i-1]['seq'] + 1} got {rec['seq']}"
                    ),
                )

            prev_hash = rec["hash"]

        return VerifyResult(ok=True, records_verified=len(records))

    # ── properties ────────────────────────────────────────────────────────────

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def last_hash(self) -> str:
        return self._last_hash

    @property
    def last_ts_ms(self) -> int:
        return self._last_ts_ms


# ── Singleton ─────────────────────────────────────────────────────────────────

_chain: "_AuditChain | None" = None
_singleton_lock = threading.Lock()


def _get_chain() -> _AuditChain:
    global _chain
    if _chain is None:
        with _singleton_lock:
            if _chain is None:
                _chain = _AuditChain()
    return _chain


# ── Public API ────────────────────────────────────────────────────────────────

def append(event_type: str, payload: dict) -> dict:
    """Append event to global audit chain. Thread-safe. Never raises."""
    return _get_chain().append(event_type, payload)


def verify_chain() -> VerifyResult:
    """Verify global audit chain integrity."""
    return _get_chain().verify()


async def push_mirror() -> None:
    """Send chain-head to Telegram as off-box tamper-evidence anchor.

    Best-effort — never raises. No-op when Telegram is not configured.
    """
    try:
        await _push_telegram()
    except Exception as exc:
        log.warning("audit_chain: Telegram mirror push failed: %s", exc)


async def _push_telegram() -> None:
    from rawos.config import settings
    import httpx

    chain = _get_chain()
    if chain.last_seq == 0:
        return  # nothing to mirror yet

    if not getattr(settings, "telegram_bot_token", None) or \
       not getattr(settings, "telegram_owner_chat_id", None):
        log.debug("audit_chain: Telegram not configured, skipping mirror push")
        return

    from datetime import datetime, timezone
    ts_utc = datetime.fromtimestamp(
        chain.last_ts_ms / 1000, tz=timezone.utc,
    ).isoformat()

    message = (
        "\U0001f510 rawos audit chain\n"
        f"seq: {chain.last_seq}\n"
        f"hash: {chain.last_hash[:32]}...\n"
        f"ts: {ts_utc}\n"
        f"server: {socket.gethostname()}"
    )

    url = (
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            url,
            json={"chat_id": settings.telegram_owner_chat_id, "text": message},
        )
    if resp.status_code != 200:
        log.warning(
            "audit_chain: Telegram mirror HTTP %s: %s",
            resp.status_code, resp.text[:200],
        )
    else:
        log.info(
            "audit_chain: mirror pushed seq=%d hash=%s...",
            chain.last_seq, chain.last_hash[:16],
        )
