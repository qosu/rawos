"""
rawos Timing Signals — Phase 10.

Computes raw timing signals from context_events and user_model.
All DB queries are synchronous (SQLite WAL mode, thread-safe).

Signals:
  dwell_minutes        — time since current domain last became active
  events_last_5min     — raw event count (activity velocity numerator)
  events_per_minute    — velocity over last 5 minutes
  domain_changed       — domain changed in last 5 min (transition signal)
  session_start        — first events after ≥30 min idle, within last 3 min
  pre_session_end      — activity rate dropped ≥60% vs prior 10 min window
  last_proactive_min   — minutes since last proactive artifact
  no_data              — True when context_events has no rows for this user
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field

import rawos.db as db

log = logging.getLogger("rawos.timing.signals")


@dataclass
class TimingSignals:
    user_id: str
    computed_at: float = field(default_factory=time.time)

    # Raw measurements
    dwell_minutes: float = 0.0
    events_last_5min: int = 0
    events_per_minute: float = 0.0
    domain_changed: bool = False
    session_start: bool = False
    pre_session_end: bool = False
    last_proactive_minutes: float = 9999.0

    # Fallback flag — when no context data, timeliness defaults to 1.0
    no_data: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


def _get_event_counts(user_id: str, now: int) -> tuple[int, int, int]:
    """
    Return (last_5min, last_10min, prev_10min) event counts for velocity signals.
    """
    with db._conn() as conn:
        def count(start: int, end: int) -> int:
            return conn.execute(
                "SELECT COUNT(*) FROM context_events WHERE user_id = ? AND ts BETWEEN ? AND ?",
                (user_id, start, end),
            ).fetchone()[0]

        last_5min  = count(now - 300,  now)
        last_10min = count(now - 600,  now)
        prev_10min = count(now - 1200, now - 600)
    return last_5min, last_10min, prev_10min


def _get_or_create_timing_state(user_id: str) -> dict:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT * FROM timing_state WHERE user_id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else {}


def _upsert_timing_state(
    user_id: str,
    last_domain: str | None = None,
    domain_changed_at: int | None = None,
    session_start_at: int | None = None,
) -> None:
    now = int(time.time())
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO timing_state (user_id, last_domain, domain_changed_at, session_start_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 last_domain       = COALESCE(excluded.last_domain,       timing_state.last_domain),
                 domain_changed_at = COALESCE(excluded.domain_changed_at, timing_state.domain_changed_at),
                 session_start_at  = COALESCE(excluded.session_start_at,  timing_state.session_start_at),
                 updated_at        = excluded.updated_at""",
            (user_id, last_domain, domain_changed_at, session_start_at, now),
        )


def compute_signals(user_id: str, current_domain: str | None = None) -> TimingSignals:
    """
    Compute all timing signals for a user.
    current_domain: the domain currently inferred (from intent engine), or None.
    """
    now = int(time.time())
    sig = TimingSignals(user_id=user_id, computed_at=float(now))

    # ------------------------------------------------------------------
    # Check if we have any context data at all
    # ------------------------------------------------------------------
    with db._conn() as conn:
        total_events = conn.execute(
            "SELECT COUNT(*) FROM context_events WHERE user_id = ?", (user_id,)
        ).fetchone()[0]

    if total_events == 0:
        sig.no_data = True
        return sig  # All timing scores will default to 0; caller uses fallback

    # ------------------------------------------------------------------
    # Event velocity signals
    # ------------------------------------------------------------------
    last_5min, last_10min, prev_10min = _get_event_counts(user_id, now)
    sig.events_last_5min = last_5min
    sig.events_per_minute = last_5min / 5.0  # events per minute over last 5 min

    # Pre-session-end: rate dropped ≥60% vs prior 10-min window
    if prev_10min > 0 and last_10min < prev_10min * 0.40:
        sig.pre_session_end = True

    # ------------------------------------------------------------------
    # Session start detection
    # ------------------------------------------------------------------
    # Session start = activity in last 3 min, but no activity 3–33 min ago
    with db._conn() as conn:
        recent_3min = conn.execute(
            "SELECT COUNT(*) FROM context_events WHERE user_id = ? AND ts >= ?",
            (user_id, now - 180),
        ).fetchone()[0]
        gap_activity = conn.execute(
            "SELECT COUNT(*) FROM context_events WHERE user_id = ? AND ts BETWEEN ? AND ?",
            (user_id, now - 1980, now - 180),
        ).fetchone()[0]

    if recent_3min > 0 and gap_activity == 0:
        sig.session_start = True
        _upsert_timing_state(user_id, session_start_at=now)

    # ------------------------------------------------------------------
    # Dwell time + domain transition
    # ------------------------------------------------------------------
    state = _get_or_create_timing_state(user_id)
    last_domain = state.get("last_domain")
    domain_changed_at = state.get("domain_changed_at") or now

    if current_domain and current_domain != last_domain:
        # Domain just changed — record transition time
        sig.domain_changed = True
        _upsert_timing_state(user_id, last_domain=current_domain, domain_changed_at=now)
        sig.dwell_minutes = 0.0
    else:
        sig.dwell_minutes = (now - domain_changed_at) / 60.0

    # Domain-changed-in-last-5-min check (separate from current check)
    if not sig.domain_changed and state.get("domain_changed_at"):
        if (now - state["domain_changed_at"]) < 300:
            sig.domain_changed = True

    # ------------------------------------------------------------------
    # Time since last proactive artifact
    # ------------------------------------------------------------------
    with db._conn() as conn:
        row = conn.execute(
            "SELECT MAX(created_at) FROM proactive_artifacts WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    last_pa = row[0] if row and row[0] else 0
    sig.last_proactive_minutes = (now - last_pa) / 60.0 if last_pa else 9999.0

    return sig
