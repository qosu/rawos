"""tests/test_output_guard.py — SHP.4 output guard anti-exfiltration tests.

Verifies that secret patterns are redacted from tool output before it
reaches the agent loop / SSE stream (I-SEC7 anti-exfiltration baseline).
"""
from __future__ import annotations

import pytest
from rawos.kernel.output_guard import guard_output


class TestSecretPatternRedaction:
    """Secret patterns must be redacted; clean output must pass through unchanged."""

    def test_stripe_live_key_redacted(self):
        text = "Found key: sk_live_ABCdefGHIjklMNOpqrSTUvwx12345678 in env"
        result = guard_output(text, "bash", "u1")
        assert "sk_live_" not in result
        assert "[REDACTED" in result

    def test_stripe_test_key_redacted(self):
        text = "key=sk_test_XYZabc123DEFghi456JKLmno789abcdefgh here"
        result = guard_output(text, "read_file", "u1")
        assert "sk_test_" not in result
        assert "[REDACTED" in result

    def test_huggingface_token_redacted(self):
        text = "HF_TOKEN=hf_ABCdefGHIjklMNOpqrSTUvwxYZ123456"
        result = guard_output(text, "bash", "u1")
        assert "hf_" not in result
        assert "[REDACTED" in result

    def test_webhook_secret_redacted(self):
        text = "STRIPE_WEBHOOK_SECRET=whsec_ABCDefghIJKLmnopQRSTuvwxYZ0123456789ab"
        result = guard_output(text, "bash", "u1")
        assert "whsec_" not in result
        assert "[REDACTED" in result

    def test_clean_output_unchanged(self):
        text = "File created at /workspace/main.py with 42 lines"
        result = guard_output(text, "write_file", "u1")
        assert result == text

    def test_short_dash_sk_not_redacted(self):
        """Short strings with 'sk-' prefix (dash, not underscore) must not be over-redacted."""
        text = "key: sk-1234"
        result = guard_output(text, "bash", "u1")
        assert "sk-1234" in result

    def test_guard_is_fail_open_on_none_input(self):
        """Guard must be fail-open — never raises, returns None unchanged."""
        result = guard_output(None, "bash", "u1")  # type: ignore[arg-type]
        assert result is None

    def test_multiple_secrets_all_redacted(self):
        """All matching secrets in a single output must each be individually redacted."""
        text = (
            "stripe=sk_live_ABCdefGHIjklMNOpqrSTUvwx12345678 "
            "hf=hf_ABCdefGHIjklMNOpqrSTUvwxYZ123456"
        )
        result = guard_output(text, "bash", "u1")
        assert "sk_live_" not in result
        assert "hf_" not in result
        assert result.count("[REDACTED") == 2
