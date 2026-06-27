"""rawos/kernel/output_guard.py — SHP.4 output guard (I-SEC7 anti-exfiltration).

Scans tool output for secret patterns before it reaches the agent loop
or SSE stream. Matches are redacted and logged at WARNING. Never raises.
"""
from __future__ import annotations

import logging
import re
from re import Pattern

log = logging.getLogger("rawos.output_guard")

# (compiled pattern, label) — ordered most-specific first to avoid partial matches
_SECRET_PATTERNS: list[tuple[Pattern[str], str]] = [
    # Stripe live/test secret keys: sk_live_<24+ alphanum> or sk_test_<24+ alphanum>
    (re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{24,}\b"), "stripe-key"),
    # Stripe webhook signing secret: whsec_<32+ alphanum>
    (re.compile(r"\bwhsec_[A-Za-z0-9]{32,}\b"), "stripe-webhook-secret"),
    # HuggingFace token: hf_<20+ alphanum>
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"), "huggingface-token"),
    # NVIDIA NIM / NGC API key: nvapi-<20+ alphanum/dash/underscore>
    (re.compile(r"\bnvapi-[A-Za-z0-9_-]{20,}\b"), "nim-key"),
    # JWT / access tokens: eyJ<50+ base64> (JSON header prefix in base64)
    (re.compile(r"\beyJ[A-Za-z0-9+/]{50,}"), "jwt-or-token"),
    # Telegram bot token: <8-12 digits>:<30+ alphanum/dash/underscore>
    (re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"), "telegram-bot-token"),
]


def guard_output(output: str | None, tool_name: str, user_id: str) -> str | None:
    """Scan tool output for secret patterns; redact matches. Never raises.

    Returns output unchanged when no patterns match.
    Returns None unchanged if input is None.
    """
    if output is None:
        return None
    if not isinstance(output, str):
        return output
    try:
        return _scan_and_redact(output, tool_name, user_id)
    except Exception as exc:
        log.error("output_guard: scanner error — returning original output: %s", exc)
        return output


def _scan_and_redact(text: str, tool_name: str, user_id: str) -> str:
    result = text
    for pattern, label in _SECRET_PATTERNS:
        matches = pattern.findall(result)
        if matches:
            log.warning(
                "output_guard: redacting %s pattern in '%s' output (user=%s, count=%d)",
                label, tool_name, user_id, len(matches),
            )
            result = pattern.sub(f"[REDACTED:{label}]", result)
    return result
