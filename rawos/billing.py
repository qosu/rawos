"""
rawos Billing — Phase 5.

Token quota enforcement, usage tracking, and Stripe subscription management.

Tier token limits (tokens/day):
  free:        50 000
  pro:        500 000
  enterprise: 10 000 000
"""
from __future__ import annotations

import logging

import rawos.db as db
from rawos.config import settings
from rawos.models import User, UserTier

log = logging.getLogger("rawos.billing")

TIER_DAILY_LIMITS: dict[str, int] = {
    UserTier.FREE.value:       50_000,
    UserTier.PRO.value:        500_000,
    UserTier.ENTERPRISE.value: 10_000_000,
}


class QuotaExceeded(Exception):
    def __init__(self, used: int, limit: int, tier: str):
        self.used  = used
        self.limit = limit
        self.tier  = tier
        super().__init__(
            f"Daily token quota exceeded ({used:,}/{limit:,} tokens for {tier} tier). "
            "Upgrade your plan or wait for daily reset."
        )


def check_quota(user_id: str, tier: str) -> None:
    user = db.get_user_by_id(user_id)
    if user is None:
        return
    limit = TIER_DAILY_LIMITS.get(tier, TIER_DAILY_LIMITS[UserTier.FREE.value])
    if user.tokens_used_today >= limit:
        raise QuotaExceeded(user.tokens_used_today, limit, tier)


def record_usage(user_id: str, tokens: int, model: str = "",
                 intent_id: str | None = None) -> None:
    if tokens <= 0:
        return
    db.consume_tokens(user_id, tokens)
    db.create_billing_event(
        user_id=user_id,
        tokens=tokens,
        model=model,
        intent_id=intent_id,
        event_type="intent",
    )
    log.debug("recorded %d tokens for user %s (intent %s)", tokens, user_id, intent_id)


# ---------------------------------------------------------------------------
# Stripe — internal helpers
# ---------------------------------------------------------------------------

def _get_stripe():
    """Returns the stripe module with api_key set. Raises if not configured."""
    if not settings.stripe_key:
        raise NotImplementedError(
            "Stripe billing is not configured. Set STRIPE_KEY in .env."
        )
    import stripe as _stripe
    _stripe.api_key = settings.stripe_key
    return _stripe


def _tier_from_price(price_id: str) -> str | None:
    """Map Stripe Price ID → rawos tier name."""
    if price_id == settings.stripe_price_pro:
        return UserTier.PRO.value
    if price_id == settings.stripe_price_enterprise:
        return UserTier.ENTERPRISE.value
    return None


def _get_or_create_stripe_customer(user: User) -> str:
    """Return existing Stripe customer ID or create a new one and persist it."""
    if user.stripe_customer_id:
        return user.stripe_customer_id
    stripe = _get_stripe()
    customer = stripe.Customer.create(
        email=user.email,
        metadata={"rawos_user_id": user.id},
    )
    db.set_stripe_customer_id(user.id, customer.id)
    log.info("created Stripe customer %s for user %s", customer.id, user.id)
    return customer.id


# ---------------------------------------------------------------------------
# Stripe — public API
# ---------------------------------------------------------------------------

def create_checkout_session(user: User, tier: str,
                             success_url: str, cancel_url: str) -> str:
    """
    Create a Stripe Checkout session for subscribing to the given tier.
    Returns the checkout URL to redirect the user to.
    """
    stripe = _get_stripe()
    price_ids = {
        UserTier.PRO.value:        settings.stripe_price_pro,
        UserTier.ENTERPRISE.value: settings.stripe_price_enterprise,
    }
    price_id = price_ids.get(tier)
    if not price_id:
        raise ValueError(f"No Stripe price configured for tier: {tier!r}")

    customer_id = _get_or_create_stripe_customer(user)

    session = stripe.checkout.Session.create(
        customer=customer_id,
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="subscription",
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=user.id,
        metadata={"tier": tier, "user_id": user.id},
    )
    log.info("checkout session %s created for user %s tier %s", session.id, user.id, tier)
    return session.url


def create_portal_session(user: User, return_url: str) -> str:
    """
    Create a Stripe Customer Portal session so the user can manage their subscription.
    Requires the user to already have a Stripe customer ID.
    """
    if not user.stripe_customer_id:
        raise ValueError(
            "No Stripe subscription found. Complete a checkout first."
        )
    stripe = _get_stripe()
    session = stripe.billing_portal.Session.create(
        customer=user.stripe_customer_id,
        return_url=return_url,
    )
    return session.url


def handle_webhook_event(payload: bytes, sig_header: str) -> dict:
    """
    Verify Stripe webhook signature and process the event.
    Raises ValueError on bad payload, PermissionError on bad signature.
    Returns {"received": True, "type": event_type}.
    """
    stripe = _get_stripe()
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except ValueError as e:
        raise ValueError(f"Invalid webhook payload: {e}") from e
    except stripe.error.SignatureVerificationError as e:
        raise PermissionError(f"Invalid webhook signature: {e}") from e

    event_type = event.type
    obj = event.data.object

    if event_type == "checkout.session.completed":
        _on_checkout_completed(obj)

    elif event_type == "customer.subscription.deleted":
        _on_subscription_deleted(obj)

    elif event_type == "customer.subscription.updated":
        _on_subscription_updated(obj)

    else:
        log.debug("unhandled webhook event: %s", event_type)

    return {"received": True, "type": event_type}


def _on_checkout_completed(session) -> None:
    user_id     = getattr(session, "client_reference_id", None)
    _meta       = getattr(session, "metadata", None)
    tier        = _meta["tier"] if _meta and "tier" in _meta else None
    customer_id = getattr(session, "customer", None)

    if not user_id or not tier:
        log.warning("checkout.session.completed missing user_id or tier: %s", session.get("id"))
        return

    if customer_id:
        db.set_stripe_customer_id(user_id, customer_id)

    db.update_user_tier(user_id, tier)
    log.info("checkout completed: user %s upgraded to %s", user_id, tier)


def _on_subscription_deleted(subscription) -> None:
    customer_id = getattr(subscription, "customer", None)
    if not customer_id:
        return
    user = db.get_user_by_stripe_customer_id(customer_id)
    if not user:
        log.warning("subscription.deleted: no user for customer %s", customer_id)
        return
    db.update_user_tier(user.id, UserTier.FREE.value)
    log.info("subscription deleted: user %s downgraded to free", user.id)


def _on_subscription_updated(subscription) -> None:
    customer_id = getattr(subscription, "customer", None)
    status      = getattr(subscription, "status", None)
    if not customer_id or status != "active":
        return
    _items_obj = getattr(subscription, "items", None)
    items = getattr(_items_obj, "data", []) if _items_obj else []
    if not items:
        return
    _price   = getattr(items[0], "price", None)
    price_id = getattr(_price, "id", None) if _price else None
    tier = _tier_from_price(price_id) if price_id else None
    if not tier:
        log.warning("subscription.updated: unknown price_id %s", price_id)
        return
    user = db.get_user_by_stripe_customer_id(customer_id)
    if not user:
        return
    db.update_user_tier(user.id, tier)
    log.info("subscription updated: user %s → %s", user.id, tier)
