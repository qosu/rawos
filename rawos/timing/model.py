"""
rawos Timing Model — Phase 10.

Converts TimingSignals into a scalar timeliness_score ∈ [0, 1].

Score components and weights:
  dwell_score        (max 0.35) — bell-curve peak at 15-25 minutes of task dwell
  velocity_score     (max 0.30) — inverse of activity velocity; penalizes flow state
  transition_score   (max 0.25) — task transition moment is high-value timing
  session_start_sc   (max 0.20) — session start is good context for primer
  pre_end_score      (max 0.20) — pre-session-end good for summary artifact

TIMELINESS_THRESHOLD = 0.35 (default, configurable in proactive scheduler).

Fallback: when signals.no_data=True, returns timeliness_score=1.0 so the
scheduler falls back to the original confidence-only gate.

Research purpose: every proactive action logs the timeliness score and all
signal values, enabling analysis of "which timing patterns correlate with
positive user ratings?" in Phase 11 and the paper.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field

from rawos.timing.signals import TimingSignals

log = logging.getLogger("rawos.timing.model")

TIMELINESS_THRESHOLD: float = 0.35


@dataclass
class TimelinessResult:
    timeliness_score: float
    dwell_score: float
    velocity_score: float
    transition_score: float
    session_start_score: float
    pre_end_score: float
    explanation: str
    fallback_mode: bool = False  # True when no context data

    def to_dict(self) -> dict:
        return {
            "timeliness_score": round(self.timeliness_score, 4),
            "components": {
                "dwell":        round(self.dwell_score, 4),
                "velocity":     round(self.velocity_score, 4),
                "transition":   round(self.transition_score, 4),
                "session_start": round(self.session_start_score, 4),
                "pre_end":      round(self.pre_end_score, 4),
            },
            "explanation": self.explanation,
            "fallback_mode": self.fallback_mode,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


def _score_dwell(minutes: float) -> float:
    """
    Bell-curve over dwell time. Sweet spot: 8–25 min.
    <3 min: too early; >45 min: long session, diminishing returns.
    """
    if minutes < 3.0:
        return 0.0
    if minutes < 8.0:
        # Linear ramp: 3 min → 0.0, 8 min → 0.35
        return 0.35 * (minutes - 3.0) / 5.0
    if minutes < 25.0:
        # Peak plateau
        return 0.35
    if minutes < 45.0:
        # Gradual decline: 25 min → 0.35, 45 min → 0.10
        return 0.35 - 0.25 * (minutes - 25.0) / 20.0
    # Long dwell — still somewhat useful
    return 0.10


def _score_velocity(events_per_minute: float) -> float:
    """
    Inverse velocity score. High velocity = flow state = do not interrupt.
    Zero velocity = idle/thinking = good moment.
    """
    if events_per_minute >= 5.0:
        return 0.0   # flow state — hard penalty
    if events_per_minute >= 2.0:
        return 0.10  # actively working
    if events_per_minute >= 0.5:
        return 0.20  # moderate pace
    return 0.30      # idle or thinking — best moment


def _score_transition(domain_changed: bool) -> float:
    """Task transitions are high-value moments for proactive assistance."""
    return 0.25 if domain_changed else 0.0


def _score_session_start(session_start: bool) -> float:
    """Session start: user re-engaging after 30+ min away."""
    return 0.20 if session_start else 0.0


def _score_pre_end(pre_session_end: bool) -> float:
    """Activity rate dropping — good time for a summary artifact."""
    return 0.20 if pre_session_end else 0.0


def _build_explanation(
    sig: TimingSignals,
    dwell: float, vel: float, trans: float, start: float, end: float,
) -> str:
    parts: list[str] = []
    if sig.no_data:
        return "no context data — fallback mode"
    if dwell > 0:
        parts.append(f"dwell {sig.dwell_minutes:.0f}min (+{dwell:.2f})")
    if vel > 0:
        parts.append(f"velocity {sig.events_per_minute:.1f}ev/min (+{vel:.2f})")
    elif sig.events_per_minute >= 5.0:
        parts.append(f"flow state {sig.events_per_minute:.1f}ev/min (0.00)")
    if trans > 0:
        parts.append(f"task transition (+{trans:.2f})")
    if start > 0:
        parts.append("session start (+{:.2f})".format(start))
    if end > 0:
        parts.append("activity declining (+{:.2f})".format(end))
    return "; ".join(parts) or "no positive timing signals"


def compute_timeliness(sig: TimingSignals) -> TimelinessResult:
    """
    Compute timeliness score from timing signals.
    Returns TimelinessResult with score and per-component breakdown.
    """
    if sig.no_data:
        # No context data → fallback: always timely (preserves pre-Phase-10 behavior)
        return TimelinessResult(
            timeliness_score=1.0,
            dwell_score=0.0, velocity_score=0.0, transition_score=0.0,
            session_start_score=0.0, pre_end_score=0.0,
            explanation="no context data — fallback mode",
            fallback_mode=True,
        )

    # Hard veto: never interrupt flow state regardless of other signals
    if sig.events_per_minute >= 5.0:
        return TimelinessResult(
            timeliness_score=0.0,
            dwell_score=0.0, velocity_score=0.0, transition_score=0.0,
            session_start_score=0.0, pre_end_score=0.0,
            explanation=f"flow state ({sig.events_per_minute:.1f} ev/min) — hard veto",
        )

    dwell = _score_dwell(sig.dwell_minutes)
    vel   = _score_velocity(sig.events_per_minute)
    trans = _score_transition(sig.domain_changed)
    start = _score_session_start(sig.session_start)
    end   = _score_pre_end(sig.pre_session_end)

    total = min(dwell + vel + trans + start + end, 1.0)
    explanation = _build_explanation(sig, dwell, vel, trans, start, end)

    log.debug(
        "timeliness user=%s score=%.3f [dwell=%.2f vel=%.2f trans=%.2f start=%.2f end=%.2f] %s",
        sig.user_id, total, dwell, vel, trans, start, end, explanation,
    )

    return TimelinessResult(
        timeliness_score=total,
        dwell_score=dwell,
        velocity_score=vel,
        transition_score=trans,
        session_start_score=start,
        pre_end_score=end,
        explanation=explanation,
    )


def get_timeliness(user_id: str, current_domain: str | None = None) -> TimelinessResult:
    """Convenience: compute signals then timeliness in one call."""
    from rawos.timing.signals import compute_signals
    sig = compute_signals(user_id, current_domain=current_domain)
    return compute_timeliness(sig)
