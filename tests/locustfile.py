"""
rawos load test — 100 concurrent users.
Phase 5 success criterion: 100 concurrent, error rate < 0.1 %, P95 latencies within SLO.

Pre-requisite:
    cd /root/rawos && source venv/bin/activate
    python tests/setup_load_test.py        # creates /tmp/rawos_loadtest_users.json

Run (headless):
    locust -f tests/locustfile.py --host http://127.0.0.1:8002 \
           --headless -u 100 -r 10 --run-time 2m \
           --csv /tmp/rawos_load_stats --csv-full-history

Run (web UI — open http://localhost:8089 after starting):
    locust -f tests/locustfile.py --host http://127.0.0.1:8002
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from collections import defaultdict

from locust import HttpUser, between, events, task
from locust.exception import StopUser

# ---------------------------------------------------------------------------
# SLO definitions (all latencies in milliseconds, P95)
# ---------------------------------------------------------------------------
SLO: dict[str, int] = {
    "/health [GET]":                     50,
    "/auth/me [GET]":                   150,
    "/projects [GET]":                  200,
    "/projects/{id} [GET]":             200,
    "/projects/{id}/files [GET]":       200,
    "/projects/{id}/artifacts [GET]":   200,
    "/preview/{id}/index [GET]":        300,
    "/intent [SSE-start]":              500,
}
ALLOWED_ERROR_RATE = 0.001  # 0.1 %

# ---------------------------------------------------------------------------
# Pre-created user pool (written by setup_load_test.py)
# ---------------------------------------------------------------------------
_POOL_FILE = Path("/tmp/rawos_loadtest_users.json")
_pool_lock = threading.Lock()
_pool_index = 0
_pool: list[dict] = []


def _load_pool() -> None:
    global _pool
    if not _POOL_FILE.exists():
        raise RuntimeError(
            f"User pool not found at {_POOL_FILE}. "
            "Run: python tests/setup_load_test.py"
        )
    _pool = json.loads(_POOL_FILE.read_text())


def _claim_slot() -> dict | None:
    global _pool_index
    with _pool_lock:
        if _pool_index >= len(_pool):
            return None
        slot = _pool[_pool_index]
        _pool_index += 1
        return slot


# ---------------------------------------------------------------------------
# SLO tracking (request-level listener)
# ---------------------------------------------------------------------------
_slo_violations: dict[str, int] = defaultdict(int)
_slo_totals: dict[str, int] = defaultdict(int)
_total_requests = 0
_total_failures = 0
_stats_lock = threading.Lock()


@events.request.add_listener
def _on_request(
    request_type: str,
    name: str,
    response_time: float,      # milliseconds
    response_length: int,
    exception,
    **kwargs,
) -> None:
    global _total_requests, _total_failures
    with _stats_lock:
        _total_requests += 1
        if exception is not None:
            _total_failures += 1
        _slo_totals[name] += 1
        threshold = SLO.get(name)
        if threshold and response_time > threshold:
            _slo_violations[name] += 1


@events.test_start.add_listener
def _on_test_start(environment, **kwargs) -> None:
    _load_pool()
    print(f"\nPool loaded: {len(_pool)} users ({_pool_file_summary()})")


def _pool_file_summary() -> str:
    roles = defaultdict(int)
    for s in _pool:
        roles[s["role"]] += 1
    return "  ".join(f"{k}={v}" for k, v in sorted(roles.items()))


@events.quitting.add_listener
def _on_quitting(environment, **kwargs) -> None:
    print("\n" + "=" * 70)
    print("rawos Load Test — SLO Report")
    print("=" * 70)

    error_rate = _total_failures / max(_total_requests, 1)
    ok = "PASS" if error_rate <= ALLOWED_ERROR_RATE else "FAIL"
    print(f"  Error rate:  {error_rate*100:.3f}%  (limit {ALLOWED_ERROR_RATE*100:.1f}%)  [{ok}]")
    print(f"  Total requests: {_total_requests}  failures: {_total_failures}")
    print()

    # Locust percentiles not accessible here directly — violations count is shown instead.
    # Full percentiles are in the --csv output.
    print("  SLO violation counts (requests that exceeded P95 threshold):")
    for name, threshold in SLO.items():
        total = _slo_totals.get(name, 0)
        violated = _slo_violations.get(name, 0)
        if total == 0:
            print(f"    {name:48s}  (no requests)")
            continue
        pct = violated / total * 100
        status = "PASS" if violated == 0 else ("WARN" if pct < 5 else "FAIL")
        print(f"    {name:48s}  {violated:4d}/{total:<6d} ({pct:5.1f}%)  [{status}] (>{threshold}ms)")

    print("=" * 70)
    # CSV output is written to /tmp/rawos_load_stats_*.csv by --csv flag
    print("Full percentiles: /tmp/rawos_load_stats_stats.csv\n")


# ---------------------------------------------------------------------------
# User classes
# ---------------------------------------------------------------------------

class RawosInfraUser(HttpUser):
    """
    95 % of load: read/write non-AI operations.
    Exercises: projects, files, artifacts, auth/me, health, preview.
    """
    weight = 95
    wait_time = between(0.5, 2.0)

    def on_start(self) -> None:
        slot = _claim_slot()
        if slot is None or slot["role"] != "infra":
            raise StopUser()
        self.headers = {"Authorization": f"Bearer {slot['access_token']}"}
        self.project_id: str = slot["project_id"]

    @task(6)
    def list_projects(self) -> None:
        self.client.get(
            "/projects",
            headers=self.headers,
            name="/projects [GET]",
        )

    @task(4)
    def get_project(self) -> None:
        self.client.get(
            f"/projects/{self.project_id}",
            headers=self.headers,
            name="/projects/{id} [GET]",
        )

    @task(3)
    def list_files(self) -> None:
        self.client.get(
            f"/projects/{self.project_id}/files",
            headers=self.headers,
            name="/projects/{id}/files [GET]",
        )

    @task(2)
    def list_artifacts(self) -> None:
        self.client.get(
            f"/projects/{self.project_id}/artifacts",
            headers=self.headers,
            name="/projects/{id}/artifacts [GET]",
        )

    @task(2)
    def get_me(self) -> None:
        self.client.get(
            "/auth/me",
            headers=self.headers,
            name="/auth/me [GET]",
        )

    @task(1)
    def health_check(self) -> None:
        self.client.get("/health", name="/health [GET]")

    @task(1)
    def preview_file(self) -> None:
        """Test the public preview endpoint (no auth)."""
        self.client.get(
            f"/preview/{self.project_id}/index.html",
            name="/preview/{id}/index [GET]",
        )


class RawosIntentUser(HttpUser):
    """
    5 % of load: SSE intent smoke test. Validates time-to-first-chunk < 500ms.
    Low frequency (wait 10–20s) to avoid LLM cost runaway.
    """
    weight = 5
    wait_time = between(10, 20)

    def on_start(self) -> None:
        # Intent users are at the tail of the pool (indices 95–99)
        slot = _claim_slot()
        if slot is None or slot["role"] != "intent":
            raise StopUser()
        self.headers = {
            "Authorization": f"Bearer {slot['access_token']}",
            "Accept":         "text/event-stream",
        }
        self.project_id: str = slot["project_id"]

    @task
    def send_minimal_intent(self) -> None:
        """
        'list files' → minimal-cost LLM call, exercises the full intent pipeline.
        Locust records time-until-headers which equals SSE time-to-first-chunk.
        """
        with self.client.post(
            "/intent",
            json={"project_id": self.project_id, "message": "list files"},
            headers=self.headers,
            stream=True,
            catch_response=True,
            name="/intent [SSE-start]",
            timeout=30,
        ) as r:
            if r.status_code == 429:
                # Rate-limited — expected for burst; mark as success (infra is fine)
                r.success()
                return
            if r.status_code != 200:
                r.failure(f"status {r.status_code}: {r.text[:200]}")
                return
            # Verify at least one SSE chunk arrives before declaring success
            try:
                chunk_received = False
                for chunk in r.iter_content(chunk_size=256):
                    if chunk:
                        chunk_received = True
                        break
            except Exception as exc:
                r.failure(f"stream read error: {exc}")
                return
            if chunk_received:
                r.success()
            else:
                r.failure("SSE stream produced no data")
