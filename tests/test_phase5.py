"""
Phase 5 tests — security, rate limiting, billing, admin, monitoring.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DEEPSEEK_KEY", "test-key")
os.environ.setdefault("JWT_SECRET",   "test-secret-long-enough-for-production-use")


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------

class TestBilling:
    def setup_method(self):
        import rawos.db as db
        self.tmp = tempfile.mkdtemp()
        db.init(str(Path(self.tmp) / "test.db"))
        from rawos.models import User, UserTier
        import rawos.db as db2
        self.db = db2
        self.user = User(id="u1", email="t@t.com", password_hash="x",
                         tier=UserTier.FREE, tokens_used_today=0)
        db2.create_user(self.user)

    def test_check_quota_passes_when_under_limit(self):
        from rawos.billing import check_quota
        check_quota("u1", "free")  # should not raise

    def test_check_quota_raises_when_over_limit(self):
        from rawos.billing import check_quota, QuotaExceeded
        from rawos.models import UserTier
        self.db.consume_tokens("u1", 50_001)  # over free limit
        with pytest.raises(QuotaExceeded) as exc_info:
            check_quota("u1", UserTier.FREE.value)
        assert exc_info.value.limit == 50_000

    def test_pro_tier_has_higher_limit(self):
        from rawos.billing import TIER_DAILY_LIMITS
        from rawos.models import UserTier
        assert TIER_DAILY_LIMITS[UserTier.PRO.value] > TIER_DAILY_LIMITS[UserTier.FREE.value]

    def test_record_usage_creates_billing_event(self):
        from rawos.billing import record_usage
        record_usage("u1", 1000, model="deepseek-chat", intent_id="i1")
        events = self.db.get_billing_events("u1")
        assert len(events) == 1
        assert events[0].tokens == 1000
        assert events[0].model == "deepseek-chat"

    def test_record_usage_increments_tokens_used_today(self):
        from rawos.billing import record_usage
        record_usage("u1", 500, model="deepseek-chat")
        user = self.db.get_user_by_id("u1")
        assert user.tokens_used_today == 500

    def test_record_usage_ignores_zero(self):
        from rawos.billing import record_usage
        record_usage("u1", 0)
        events = self.db.get_billing_events("u1")
        assert len(events) == 0



# ---------------------------------------------------------------------------
# Admin DB functions
# ---------------------------------------------------------------------------

class TestAdminDB:
    def setup_method(self):
        import rawos.db as db
        self.tmp = tempfile.mkdtemp()
        db.init(str(Path(self.tmp) / "test.db"))
        from rawos.models import User, UserTier
        import rawos.db as db2
        self.db = db2
        self.user = User(id="u1", email="t@t.com", password_hash="x", tier=UserTier.FREE)
        db2.create_user(self.user)

    def test_get_all_users_returns_list(self):
        users = self.db.get_all_users()
        assert len(users) == 1
        assert users[0].email == "t@t.com"

    def test_set_admin_flag(self):
        self.db.set_admin("u1", True)
        user = self.db.get_user_by_id("u1")
        assert user.is_admin is True

    def test_unset_admin_flag(self):
        self.db.set_admin("u1", True)
        self.db.set_admin("u1", False)
        user = self.db.get_user_by_id("u1")
        assert user.is_admin is False

    def test_is_admin_defaults_false(self):
        user = self.db.get_user_by_id("u1")
        assert user.is_admin is False

    def test_get_admin_stats_returns_dict(self):
        stats = self.db.get_admin_stats()
        assert "users_total" in stats
        assert "intents_today" in stats
        assert stats["users_total"] == 1


# ---------------------------------------------------------------------------
# Admin Routes
# ---------------------------------------------------------------------------

class TestAdminRoutes:
    def setup_method(self):
        import rawos.db as db
        self.tmp = tempfile.mkdtemp()
        db.init(str(Path(self.tmp) / "test.db"))
        from rawos.models import User, UserTier
        import rawos.db as db2
        self.db = db2
        self.admin = User(id="admin1", email="admin@t.com", password_hash="x",
                          tier=UserTier.FREE, is_admin=True)
        self.nonadmin = User(id="user1", email="user@t.com", password_hash="x",
                             tier=UserTier.FREE, is_admin=False)
        db2.create_user(self.admin)
        db2.create_user(self.nonadmin)

    def _app(self, user_id: str):
        from fastapi.testclient import TestClient
        from rawos.api.app import app
        from rawos.api.deps import current_user
        import rawos.db as db2
        user = db2.get_user_by_id(user_id)
        app.dependency_overrides[current_user] = lambda: user
        return TestClient(app)

    def test_admin_stats_accessible_to_admin(self):
        client = self._app("admin1")
        resp = client.get("/admin/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "users_total" in data

    def test_admin_stats_forbidden_for_non_admin(self):
        client = self._app("user1")
        resp = client.get("/admin/stats")
        assert resp.status_code == 403

    def test_admin_users_accessible_to_admin(self):
        client = self._app("admin1")
        resp = client.get("/admin/users")
        assert resp.status_code == 200
        users = resp.json()
        assert isinstance(users, list)
        assert len(users) == 2

    def test_admin_users_forbidden_for_non_admin(self):
        client = self._app("user1")
        resp = client.get("/admin/users")
        assert resp.status_code == 403

    def test_set_admin_flag_via_api(self):
        client = self._app("admin1")
        resp = client.post("/admin/users/user1/set-admin?is_admin=true")
        assert resp.status_code == 200
        import rawos.db as db2
        user = db2.get_user_by_id("user1")
        assert user.is_admin is True


# ---------------------------------------------------------------------------
# Rate Limiter
# ---------------------------------------------------------------------------

class TestRateLimiter:
    def test_classify_auth_endpoint(self):
        from rawos.middleware.rate_limiter import _classify_endpoint
        assert _classify_endpoint("/auth/login") == "auth"
        assert _classify_endpoint("/auth/signup") == "auth"

    def test_classify_intent_endpoint(self):
        from rawos.middleware.rate_limiter import _classify_endpoint
        assert _classify_endpoint("/intent") == "intent"

    def test_classify_api_endpoint(self):
        from rawos.middleware.rate_limiter import _classify_endpoint
        assert _classify_endpoint("/projects/abc/memories") == "api"
        assert _classify_endpoint("/admin/stats") == "api"

    def test_get_limit_for_auth(self):
        from rawos.middleware.rate_limiter import RateLimiterMiddleware
        from rawos.config import settings
        middleware = RateLimiterMiddleware(app=MagicMock())
        limit, window = middleware._get_limit("auth")
        assert limit == settings.rate_limit_auth_rpm
        assert window == 60

    def test_get_limit_for_intent(self):
        from rawos.middleware.rate_limiter import RateLimiterMiddleware
        from rawos.config import settings
        middleware = RateLimiterMiddleware(app=MagicMock())
        limit, window = middleware._get_limit("intent")
        assert limit == settings.rate_limit_intent_rpm

    def test_rate_limit_allows_when_redis_down(self):
        """Rate limiter must fail open (allow) when Redis is unavailable."""
        async def _run():
            from rawos.middleware.rate_limiter import _check_rate_limit
            with patch("rawos.middleware.rate_limiter._get_redis") as mock_redis:
                mock_redis.return_value.pipeline.side_effect = Exception("Redis down")
                allowed, retry_after = await _check_rate_limit("key", 10)
                return allowed, retry_after
        allowed, retry_after = asyncio.run(_run())
        assert allowed is True


# ---------------------------------------------------------------------------
# Security config
# ---------------------------------------------------------------------------

class TestSecurityConfig:
    def test_allowed_origins_not_wildcard(self):
        from rawos.config import settings
        assert "*" not in settings.allowed_origins, "CORS wildcard not allowed in production"

    def test_redis_url_configured(self):
        from rawos.config import settings
        assert settings.redis_url.startswith("redis://")

    def test_rate_limit_settings_are_positive(self):
        from rawos.config import settings
        assert settings.rate_limit_auth_rpm > 0
        assert settings.rate_limit_intent_rpm > 0
        assert settings.rate_limit_api_rpm > 0


# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------

class TestMonitoring:
    def test_metrics_defined(self):
        from rawos.monitoring import (
            http_requests_total, intent_tokens_total,
            errors_total, active_sse_connections,
        )
        # Verify they're callable/incrementable
        http_requests_total.labels(method="GET", endpoint_group="health", status_code="200").inc()
        errors_total.labels(error_type="test").inc()
        active_sse_connections.inc()
        active_sse_connections.dec()

    def test_endpoint_group_classification(self):
        from rawos.monitoring import _endpoint_group
        assert _endpoint_group("/auth/login")      == "auth"
        assert _endpoint_group("/intent")          == "intent"
        assert _endpoint_group("/projects/abc")    == "projects"
        assert _endpoint_group("/admin/stats")     == "admin"
        assert _endpoint_group("/health")          == "health"
        assert _endpoint_group("/something-else")  == "other"
