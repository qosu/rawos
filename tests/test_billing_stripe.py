"""Phase 5 — Stripe billing integration tests."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DEEPSEEK_KEY", "test-key")
os.environ.setdefault("JWT_SECRET",   "test-secret-long-enough-for-production-use")

from fastapi.testclient import TestClient
from rawos.api.app import app
from rawos.models import User, UserTier
import rawos.db as db

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    os.environ["DB_PATH"] = str(tmp_path / "test.db")
    os.environ["WORKSPACES_ROOT"] = str(tmp_path / "ws")
    db.init(os.environ["DB_PATH"])
    yield


def _register(email: str = "u@test.com") -> dict:
    r = client.post("/auth/signup", json={"email": email, "password": "password123"})
    assert r.status_code == 201
    return r.json()


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

class TestUserTierDB:
    def test_free_to_pro(self):
        u = db.create_user(User(email="a@t.com", password_hash="x",
                                tier=UserTier.FREE, token_budget_daily=50_000))
        db.update_user_tier(u.id, "pro")
        updated = db.get_user_by_id(u.id)
        assert updated.tier == UserTier.PRO
        assert updated.token_budget_daily == 500_000

    def test_pro_to_free(self):
        u = db.create_user(User(email="b@t.com", password_hash="x",
                                tier=UserTier.PRO, token_budget_daily=500_000))
        db.update_user_tier(u.id, "free")
        updated = db.get_user_by_id(u.id)
        assert updated.tier == UserTier.FREE
        assert updated.token_budget_daily == 50_000

    def test_set_and_lookup_stripe_customer(self):
        u = db.create_user(User(email="c@t.com", password_hash="x",
                                tier=UserTier.FREE, token_budget_daily=50_000))
        db.set_stripe_customer_id(u.id, "cus_abc123")
        found = db.get_user_by_stripe_customer_id("cus_abc123")
        assert found is not None
        assert found.id == u.id
        assert found.stripe_customer_id == "cus_abc123"

    def test_lookup_unknown_customer_returns_none(self):
        assert db.get_user_by_stripe_customer_id("cus_unknown") is None


# ---------------------------------------------------------------------------
# /billing/status
# ---------------------------------------------------------------------------

class TestBillingStatus:
    def test_returns_status_for_free_user(self):
        tokens = _register("status@t.com")
        r = client.get("/billing/status",
                       headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert r.status_code == 200
        d = r.json()
        assert d["tier"] == "free"
        assert d["token_limit_daily"] == 50_000
        assert "tokens_used_today" in d
        assert "has_subscription" in d

    def test_requires_auth(self):
        r = client.get("/billing/status")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# /billing/checkout
# ---------------------------------------------------------------------------

class TestCheckout:
    def test_requires_auth(self):
        r = client.post("/billing/checkout", json={"tier": "pro"})
        assert r.status_code == 401

    def test_invalid_tier_rejected(self):
        tokens = _register("co_bad@t.com")
        r = client.post("/billing/checkout", json={"tier": "superduper"},
                        headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert r.status_code == 400

    def test_already_on_tier_rejected(self):
        tokens = _register("co_same@t.com")
        r = client.post("/billing/checkout", json={"tier": "free"},
                        headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert r.status_code == 400

    @patch("rawos.billing._get_stripe")
    def test_returns_checkout_url(self, mock_get_stripe):
        mock_stripe = MagicMock()
        mock_stripe.Customer.create.return_value = MagicMock(id="cus_test_new")
        mock_stripe.checkout.Session.create.return_value = MagicMock(
            id="cs_test_xyz",
            url="https://checkout.stripe.com/pay/cs_test_xyz",
        )
        mock_get_stripe.return_value = mock_stripe

        tokens = _register("co_ok@t.com")
        r = client.post("/billing/checkout", json={"tier": "pro"},
                        headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert r.status_code == 200
        assert "stripe.com" in r.json()["url"]


# ---------------------------------------------------------------------------
# /billing/webhook
# ---------------------------------------------------------------------------

class TestWebhook:
    def test_missing_signature_returns_400(self):
        r = client.post("/billing/webhook",
                        content=b'{"type":"test"}',
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 400

    @patch("rawos.billing._get_stripe")
    def test_checkout_completed_upgrades_user(self, mock_get_stripe):
        mock_stripe = MagicMock()
        payload = json.dumps({
            "type": "checkout.session.completed",
            "data": {"object": {}},  # will be overridden by construct_event
        }).encode()

        u = db.create_user(User(email="wh_up@t.com", password_hash="x",
                                tier=UserTier.FREE, token_budget_daily=50_000))

        # construct_event returns the real event dict directly
        session_obj = MagicMock()
        session_obj.client_reference_id = u.id
        session_obj.customer = "cus_wh_test"
        session_obj.metadata = {"tier": "pro", "user_id": u.id}
        event_obj = MagicMock()
        event_obj.type = "checkout.session.completed"
        event_obj.data.object = session_obj
        mock_stripe.Webhook.construct_event.return_value = event_obj
        mock_get_stripe.return_value = mock_stripe

        r = client.post("/billing/webhook", content=payload,
                        headers={"stripe-signature": "t=1,v1=sig",
                                 "Content-Type": "application/json"})
        assert r.status_code == 200
        upgraded = db.get_user_by_id(u.id)
        assert upgraded.tier == UserTier.PRO
        assert upgraded.stripe_customer_id == "cus_wh_test"

    @patch("rawos.billing._get_stripe")
    def test_subscription_deleted_downgrades_user(self, mock_get_stripe):
        mock_stripe = MagicMock()
        u = db.create_user(User(email="wh_dn@t.com", password_hash="x",
                                tier=UserTier.PRO, token_budget_daily=500_000))
        db.set_stripe_customer_id(u.id, "cus_to_cancel")

        sub_obj = MagicMock()
        sub_obj.customer = "cus_to_cancel"
        event_obj2 = MagicMock()
        event_obj2.type = "customer.subscription.deleted"
        event_obj2.data.object = sub_obj
        mock_stripe.Webhook.construct_event.return_value = event_obj2
        mock_get_stripe.return_value = mock_stripe

        payload = b'{"type":"customer.subscription.deleted","data":{"object":{}}}'
        r = client.post("/billing/webhook", content=payload,
                        headers={"stripe-signature": "t=1,v1=sig",
                                 "Content-Type": "application/json"})
        assert r.status_code == 200
        downgraded = db.get_user_by_id(u.id)
        assert downgraded.tier == UserTier.FREE


# ---------------------------------------------------------------------------
# /billing/portal
# ---------------------------------------------------------------------------

class TestPortal:
    def test_requires_auth(self):
        r = client.post("/billing/portal")
        assert r.status_code == 401

    def test_no_customer_returns_400(self):
        tokens = _register("portal_no@t.com")
        r = client.post("/billing/portal",
                        headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert r.status_code == 400

    @patch("rawos.billing._get_stripe")
    def test_returns_portal_url(self, mock_get_stripe):
        mock_stripe = MagicMock()
        mock_stripe.billing_portal.Session.create.return_value = MagicMock(
            url="https://billing.stripe.com/session/test"
        )
        mock_get_stripe.return_value = mock_stripe

        tokens = _register("portal_ok@t.com")
        uid = client.get("/auth/me",
                         headers={"Authorization": f"Bearer {tokens['access_token']}"}).json()["id"]
        db.set_stripe_customer_id(uid, "cus_portal_ok")

        r = client.post("/billing/portal",
                        headers={"Authorization": f"Bearer {tokens['access_token']}"})
        assert r.status_code == 200
        assert "billing.stripe.com" in r.json()["url"]
