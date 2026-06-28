"""
anima Rate Limiter — Phase 5.

Redis-based sliding window rate limiter implemented as a Starlette middleware.

Rate limit groups:
  auth    — 5 req/min per IP (signup, login endpoints)
  intent  — 10 req/min per authenticated user_id
  api     — 120 req/min per authenticated user_id (all other endpoints)

Returns 429 Too Many Requests with Retry-After header when limit exceeded.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from anima.config import settings
from anima import monitoring

log = logging.getLogger("anima.rate_limiter")

_redis_client = None


def _get_redis():
    global _redis_client
    if _redis_client is None:
        import redis.asyncio as aioredis
        _redis_client = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=1.0,
            socket_timeout=1.0,
        )
    return _redis_client


# Auth-throttled paths: signup and login only.
# /auth/me, /auth/refresh etc. are authenticated calls → api bucket (per user_id).
_AUTH_THROTTLED = frozenset(["/auth/signup", "/auth/login"])
# Public paths exempt from per-user API rate limiting (no auth to key on).
_RATE_LIMIT_EXEMPT = frozenset(["/health", "/metrics"])
_RATE_LIMIT_EXEMPT_PREFIX = ("/preview/",)


def _classify_endpoint(path: str) -> str:
    """Return rate limit group for a given request path."""
    if path in _AUTH_THROTTLED:
        return "auth"
    if any(path.startswith(p) for p in _RATE_LIMIT_EXEMPT_PREFIX):
        return "exempt"
    if path in _RATE_LIMIT_EXEMPT:
        return "exempt"
    if path.startswith("/intent"):
        return "intent"
    return "api"


async def _check_rate_limit(key: str, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
    """
    Sliding window counter via Redis INCR + EXPIRE.
    Returns (is_allowed, retry_after_seconds).
    Falls back to allowing if Redis is unavailable.
    """
    try:
        r = _get_redis()
        pipe = r.pipeline(transaction=False)
        pipe.incr(key)
        pipe.ttl(key)
        count, ttl = await pipe.execute()
        if ttl < 0:
            # Key exists but has no TTL — set it now
            await r.expire(key, window_seconds)
            ttl = window_seconds
        if count == 1:
            # First request in this window — set TTL
            await r.expire(key, window_seconds)
            ttl = window_seconds
        if count > limit:
            return False, max(ttl, 1)
        return True, 0
    except Exception as e:
        log.warning("rate limiter Redis error (failing open): %s", e)
        return True, 0  # fail open — never block due to Redis being down


class RateLimiterMiddleware(BaseHTTPMiddleware):
    """
    Per-endpoint rate limiting.
    - Auth endpoints: limited per client IP
    - Intent + API endpoints: limited per authenticated user_id (falls back to IP)
    """

    EXEMPT_PATHS = {"/health", "/metrics", "/docs", "/openapi.json", "/billing/webhook"}

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        import os
        if os.environ.get("RAWOS_TESTING"):
            return await call_next(request)

        path = request.url.path

        if path in self.EXEMPT_PATHS:
            return await call_next(request)

        group = _classify_endpoint(path)

        if group == "exempt":
            return await call_next(request)

        # Determine subject: user_id from JWT or client IP
        subject = self._get_subject(request, group)

        limit, window = self._get_limit(group)
        key = f"rl:{group}:{subject}"

        allowed, retry_after = await _check_rate_limit(key, limit, window)

        if not allowed:
            monitoring.rate_limit_hits_total.labels(endpoint_group=group).inc()
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Rate limit exceeded. Too many requests.",
                    "retry_after": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)

    def _get_subject(self, request: Request, group: str) -> str:
        """Extract user_id from Bearer token, fall back to client IP."""
        if group == "auth":
            return self._client_ip(request)
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
            try:
                from jose import jwt
                payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
                return payload.get("sub", self._client_ip(request))
            except Exception:
                pass
        return self._client_ip(request)

    def _client_ip(self, request: Request) -> str:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _get_limit(self, group: str) -> tuple[int, int]:
        """Returns (limit, window_seconds)."""
        return {
            "auth":   (settings.rate_limit_auth_rpm,   60),
            "intent": (settings.rate_limit_intent_rpm,  60),
            "api":    (settings.rate_limit_api_rpm,     60),
        }.get(group, (settings.rate_limit_api_rpm, 60))
