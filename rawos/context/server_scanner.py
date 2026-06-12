"""
rawos Server Scanner — autonomous server state collection.

rawos scans the ENTIRE server independently. No human trigger required.
Sources: systemd service failures, critical log errors, resource pressure.
Returns severity-ranked anomalies rawos can act on.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from rawos.kernel.arch import get_arch

# Map known service names to their source repos (for workdir resolution)
_SERVICE_TO_REPO: dict[str, str] = {
    "exocortex":          "/root/exocortex",
    "rawos":              "/root/rawos",
    "sovereign":          "/root/sovereign",
    "prometheus-nimgen":  "/root/prometheus",
    "prometheus-status":  "/root/prometheus",
    "knowforge-game-bot": "/root/brain",
    "knowforge-tele-bot": "/root/brain",
    "exocortex-bot":      "/root/exocortex",
    "extensionn":         "/root/neurovm",
    "research-foundry":   "/root/liveproof-agent",
    "liveproof-agent":    "/root/liveproof-agent",
}

# Services rawos actively monitors for recent errors
_MONITORED_SERVICES = list(_SERVICE_TO_REPO.keys())

# Severity thresholds
SEVERITY_CRITICAL = 9   # disk > 90%, service in FAILED state
SEVERITY_HIGH     = 7   # service recently crashed and recovered
SEVERITY_MEDIUM   = 6   # recent ERROR logs in monitored service
SEVERITY_LOW      = 3   # informational


@dataclass
class ServerAnomaly:
    kind: str           # service_failed | service_error | disk_critical | disk_warning
    affected_path: str  # repo path for code anomalies, "/" for disk, service unit for failures
    service: str        # systemd service name, empty for resource anomalies
    detail: str         # human-readable description
    last_log: str       # relevant log lines for agent context
    severity: int       # 1-10

    @property
    def domain(self) -> str:
        """Canonical (kind[:service]) key for autonomy_track_record / cooldown lookups."""
        return f"{self.kind}:{self.service}" if self.service else self.kind

    def to_trigger_ctx(self) -> dict:
        return {
            "repo_root":       self.affected_path,  # _resolve_proactive_workdir uses this
            "anomaly_kind":    self.kind,
            "anomaly_detail":  self.detail,
            "service":         self.service,
            "last_log":        self.last_log[:800],
            "severity":        self.severity,
            "domain":          self.domain,
        }

    def to_context_summary(self) -> str:
        parts = [
            f"[SERVER_SCAN — autonomous rawos observation]",
            f"Anomaly type: {self.kind}",
            f"Detail: {self.detail}",
        ]
        if self.service:
            parts.append(f"Service: {self.service}")
        if self.last_log:
            parts.append(f"\nRecent logs:\n{self.last_log}")
        return "\n".join(parts)


@dataclass
class ServerStateSnapshot:
    ts: int
    anomalies: list[ServerAnomaly] = field(default_factory=list)

    @property
    def actionable(self) -> list[ServerAnomaly]:
        """Anomalies sorted by severity descending."""
        return sorted(self.anomalies, key=lambda a: a.severity, reverse=True)

    @property
    def max_severity(self) -> int:
        return self.anomalies[0].severity if self.anomalies else 0


def collect_server_state() -> ServerStateSnapshot:
    """
    Synchronous collection — call via run_in_executor.
    Collects: failed services, recent critical errors, resource pressure.
    """
    anomalies: list[ServerAnomaly] = []
    anomalies.extend(_check_failed_services())
    anomalies.extend(_check_recent_errors())
    anomalies.extend(_check_resources())
    return ServerStateSnapshot(ts=int(time.time()), anomalies=anomalies)


def _check_failed_services() -> list[ServerAnomaly]:
    """Detect systemd services currently in FAILED state. Severity 8."""
    try:
        arch = get_arch()
        anomalies = []
        for service in arch.service_manager.list_failed():
            name = service.removesuffix(".service")
            last_log = arch.log_reader.tail(service, 8)[-600:]
            repo = _SERVICE_TO_REPO.get(name, "/root")
            anomalies.append(ServerAnomaly(
                kind="service_failed",
                affected_path=repo,
                service=service,
                detail=f"{service} is in FAILED state — needs immediate diagnosis",
                last_log=last_log,
                severity=8,
            ))
        return anomalies
    except Exception:
        return []


def _check_recent_errors() -> list[ServerAnomaly]:
    """Detect ERROR/CRITICAL logs in monitored services in the last 15 minutes. Severity 6."""
    anomalies = []
    arch = get_arch()
    for svc_name in _MONITORED_SERVICES:
        svc_unit = f"{svc_name}.service"
        try:
            output = arch.log_reader.recent_errors(svc_unit, "15 minutes ago")
            if output:
                repo = _SERVICE_TO_REPO.get(svc_name, "/root")
                anomalies.append(ServerAnomaly(
                    kind="service_error",
                    affected_path=repo,
                    service=svc_unit,
                    detail=f"Recent ERROR logs in {svc_unit} (last 15 min)",
                    last_log=output[-800:],
                    severity=6,
                ))
        except Exception:
            continue
    return anomalies


def _check_resources() -> list[ServerAnomaly]:
    """Detect disk pressure. Severity 9 (≥90%) or 6 (≥85%)."""
    anomalies = []
    try:
        pct = get_arch().resource_probe.disk_percent("/")
        if pct is not None:
            if pct >= 90:
                anomalies.append(ServerAnomaly(
                    kind="disk_critical",
                    affected_path="/root",
                    service="",
                    detail=f"Disk at {pct}% — critical (≥90%). Immediate action required.",
                    last_log="",
                    severity=9,
                ))
            elif pct >= 85:
                anomalies.append(ServerAnomaly(
                    kind="disk_warning",
                    affected_path="/root",
                    service="",
                    detail=f"Disk at {pct}% — warning (≥85%). Review large files/logs.",
                    last_log="",
                    severity=6,
                ))
    except Exception:
        pass
    return anomalies
