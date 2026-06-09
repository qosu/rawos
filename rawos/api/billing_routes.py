"""rawos Billing API — Phase 5. Stripe checkout, webhook, and portal endpoints."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from rawos import billing
from rawos.api.deps import current_user
from rawos.models import User, UserTier

log = logging.getLogger("rawos.billing_routes")
router = APIRouter(prefix="/billing", tags=["billing"])

_BASE_URL = "https://downgrade.app"


class CheckoutRequest(BaseModel):
    tier: str


@router.post("/checkout")
async def create_checkout(body: CheckoutRequest, user: User = Depends(current_user)) -> dict:
    """Create a Stripe Checkout session. Returns {url} to redirect user."""
    if body.tier not in (UserTier.PRO.value, UserTier.ENTERPRISE.value):
        raise HTTPException(status_code=400, detail=f"Invalid tier: {body.tier!r}")
    if user.tier.value == body.tier:
        raise HTTPException(status_code=400, detail=f"Already on {body.tier} tier")

    try:
        url = billing.create_checkout_session(
            user=user,
            tier=body.tier,
            success_url=f"{_BASE_URL}/billing?success=1",
            cancel_url=f"{_BASE_URL}/billing",
        )
    except NotImplementedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"url": url}


@router.post("/webhook")
async def stripe_webhook(request: Request) -> dict:
    """
    Stripe webhook endpoint. Uses raw body for signature verification.
    No authentication — Stripe calls this directly.
    """
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    if not sig_header:
        raise HTTPException(status_code=400, detail="Missing stripe-signature header")

    try:
        result = billing.handle_webhook_event(payload, sig_header)
    except ValueError as e:
        log.warning("webhook bad payload: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except PermissionError as e:
        log.warning("webhook bad signature: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.exception("webhook processing error: %s", e)
        raise HTTPException(status_code=500, detail="webhook processing failed")

    return result


@router.post("/portal")
async def billing_portal(user: User = Depends(current_user)) -> dict:
    """Create a Stripe Customer Portal session. Returns {url} to redirect user."""
    try:
        url = billing.create_portal_session(
            user=user,
            return_url=f"{_BASE_URL}/billing",
        )
    except NotImplementedError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"url": url}


@router.get("/status")
async def billing_status(user: User = Depends(current_user)) -> dict:
    """Return current billing status for the authenticated user."""
    from rawos.billing import TIER_DAILY_LIMITS
    limit = TIER_DAILY_LIMITS.get(user.tier.value, 50_000)
    return {
        "tier":               user.tier.value,
        "tokens_used_today":  user.tokens_used_today,
        "token_limit_daily":  limit,
        "has_subscription":   user.stripe_customer_id is not None,
    }
