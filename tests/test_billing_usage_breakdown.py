"""Per-call DeepSeek usage breakdown persisted to billing_events for cost reporting.
See agent_loop._log_usage / billing_context / db.create_billing_event."""
from __future__ import annotations

import os
import pytest

os.environ.setdefault("DEEPSEEK_KEY", "test-key")
os.environ.setdefault("JWT_SECRET", "test-secret-long-enough-for-production-use")

import rawos.db as db
from rawos.models import User
from rawos.kernel import billing_context
from rawos.kernel.agent_loop import _compute_cost_usd_micros, _log_usage


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    os.environ["DB_PATH"] = str(tmp_path / "test.db")
    db.init(os.environ["DB_PATH"])
    yield


def test_compute_cost_usd_micros_known_model():
    usage = {
        "prompt_cache_hit_tokens": 1_000_000,
        "prompt_cache_miss_tokens": 1_000_000,
        "completion_tokens": 1_000_000,
    }
    cost = _compute_cost_usd_micros("deepseek-chat", usage)
    assert cost == round(0.003625 * 1_000_000 + 0.435 * 1_000_000 + 0.87 * 1_000_000)


def test_compute_cost_usd_micros_unknown_model_returns_none():
    usage = {
        "prompt_cache_hit_tokens": 100,
        "prompt_cache_miss_tokens": 100,
        "completion_tokens": 100,
    }
    assert _compute_cost_usd_micros("deepseek-v4-flash", usage) is None


def test_log_usage_without_context_does_not_write_billing_event():
    usage = {
        "prompt_tokens": 100,
        "prompt_cache_hit_tokens": 60,
        "prompt_cache_miss_tokens": 40,
        "completion_tokens": 20,
    }
    _log_usage("deepseek-chat", usage)
    assert db.get_billing_events("any-user") == []


def test_log_usage_with_context_writes_billing_event_with_breakdown():
    user = db.create_user(User(email="u@test.com", password_hash="x"))
    usage = {
        "prompt_tokens": 100,
        "prompt_cache_hit_tokens": 60,
        "prompt_cache_miss_tokens": 40,
        "completion_tokens": 20,
    }
    with billing_context.set_billing_context(user_id=user.id, intent_id="intent-1", event_type="server_scan"):
        _log_usage("deepseek-chat", usage)

    events = db.get_billing_events(user.id)
    assert len(events) == 1
    ev = events[0]
    assert ev.intent_id == "intent-1"
    assert ev.event_type == "server_scan"
    assert ev.cache_hit_tokens == 60
    assert ev.cache_miss_tokens == 40
    assert ev.output_tokens == 20
    assert ev.cost_usd_micros == round(60 * 0.003625 + 40 * 0.435 + 20 * 0.87)
