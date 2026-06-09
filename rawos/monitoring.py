"""
rawos Prometheus metrics — Phase 5.

Exposes /metrics endpoint (Prometheus text format).
Provides counters/histograms consumed throughout the application.
"""
from __future__ import annotations

import time
from typing import Awaitable, Callable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    REGISTRY,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------

http_requests_total = Counter(
    "rawos_http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint_group", "status_code"],
)

http_request_duration_seconds = Histogram(
    "rawos_http_request_duration_seconds",
    "HTTP request duration",
    ["endpoint_group"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)

intent_tokens_total = Counter(
    "rawos_intent_tokens_total",
    "Tokens consumed per intent execution",
    ["model", "user_tier"],
)

intent_duration_seconds = Histogram(
    "rawos_intent_duration_seconds",
    "Total wall time for intent execution (including streaming)",
    ["model"],
    buckets=(1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0),
)

active_sse_connections = Gauge(
    "rawos_active_sse_connections",
    "Current number of open SSE connections",
)

errors_total = Counter(
    "rawos_errors_total",
    "Application errors by type",
    ["error_type"],
)

agent_spawns_total = Counter(
    "rawos_agent_spawns_total",
    "Sub-agents spawned by orchestrator",
    ["agent_type"],
)

rate_limit_hits_total = Counter(
    "rawos_rate_limit_hits_total",
    "Rate limit exceeded events",
    ["endpoint_group"],
)


# ---------------------------------------------------------------------------
# HTTP middleware for request metrics
# ---------------------------------------------------------------------------

def _endpoint_group(path: str) -> str:
    if path.startswith("/auth"):   return "auth"
    if path.startswith("/intent"): return "intent"
    if path.startswith("/projects"): return "projects"
    if path.startswith("/admin"):  return "admin"
    if path == "/health":          return "health"
    return "other"


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        group = _endpoint_group(request.url.path)
        start = time.perf_counter()
        try:
            response = await call_next(request)
            status = str(response.status_code)
        except Exception:
            status = "500"
            raise
        finally:
            duration = time.perf_counter() - start
            http_requests_total.labels(
                method=request.method,
                endpoint_group=group,
                status_code=status,
            ).inc()
            http_request_duration_seconds.labels(endpoint_group=group).observe(duration)
        return response

# ---------------------------------------------------------------------------
# Evaluation metrics — Phase 7
# ---------------------------------------------------------------------------

inference_total = Counter(
    'rawos_inference_total',
    'Total intent inferences produced by the engine',
    ['source', 'domain'],
)

inference_rated_correct_total = Counter(
    'rawos_inference_rated_correct_total',
    'Inferences rated correct (artifact rating >= 3)',
)

inference_rated_incorrect_total = Counter(
    'rawos_inference_rated_incorrect_total',
    'Inferences rated incorrect (artifact rating < 3)',
)

artifact_rating_total = Counter(
    'rawos_artifact_rating_total',
    'Artifact ratings submitted by users',
    ['rating'],
)

artifact_relevance_mean = Gauge(
    'rawos_artifact_relevance_mean',
    'Rolling mean relevance score of proactive artifacts (1–5 scale)',
)
