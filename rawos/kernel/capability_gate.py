"""rawos/kernel/capability_gate.py — SHP.4 I-SEC6 unified capability gate.

Single choke point for all tool executions. Classifies each action into a
CapabilityTier, audits the call, and returns a GateResult.

SHP.4 policy (I-SEC11 audit-first): all tiers ALLOWED, all calls AUDITED.
TIER3 additionally logged at WARNING level for operator visibility.
SHP.6: flip TIER3 to DENY after audit bake-in period confirms no false positives.
"""
from __future__ import annotations

import dataclasses
import enum
import logging

log = logging.getLogger("rawos.capability_gate")

class CapabilityTier(enum.IntEnum):
    """Privilege classification for rawos tool actions."""

    TIER0 = 0  # Read-only / sandboxed output — no mutation
    TIER1 = 1  # Mutating within isolated user workspace
    TIER2 = 2  # Privileged ops on rawos source / system resources — graduation-gated
    TIER3 = 3  # System-level / perimeter / authentication — always human-gated

# manage_pam = PAM authentication config; always TIER3 regardless of action
_ALWAYS_TIER3: frozenset[str] = frozenset({"manage_pam"})

# manage_service svc_action values that are TIER3 (disruptive to running system)
_TIER3_SVC_ACTIONS: frozenset[str] = frozenset({"restart", "stop", "start"})

_TIER2_TOOLS: frozenset[str] = frozenset({
    "manage_file",
    "manage_owned_resource",
    "manage_venv",
    "git_commit",
})

_TIER1_TOOLS: frozenset[str] = frozenset({
    "bash",
    "write_file",
    "git_branch",
})

_TIER0_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "list_files",
    "bash_readonly",
    "fetch_url",
    "deploy",
})

@dataclasses.dataclass(frozen=True)
class GateResult:
    allowed: bool
    tier: CapabilityTier
    reason: str
    tool_name: str
    user_id: str

def classify_tier(tool_name: str, params: dict) -> CapabilityTier:
    """Classify minimum capability tier required for this tool call.

    Fast and non-raising — called synchronously before every tool execution.
    """
    if tool_name in _ALWAYS_TIER3:
        return CapabilityTier.TIER3

    if tool_name == "manage_service":
        action = params.get("action", "")
        if action in ("action", "approved_apply"):
            svc_action = params.get("svc_action") or params.get("operation", "")
            if svc_action in _TIER3_SVC_ACTIONS:
                return CapabilityTier.TIER3
        return CapabilityTier.TIER2

    if tool_name in _TIER2_TOOLS:
        return CapabilityTier.TIER2
    if tool_name in _TIER1_TOOLS:
        return CapabilityTier.TIER1
    if tool_name in _TIER0_TOOLS:
        return CapabilityTier.TIER0

    # Unknown tools: TIER0 — execute() blocks unknown tools before gate matters
    return CapabilityTier.TIER0

def pre_execute_gate(
    tool_name: str,
    params: dict,
    workdir: str,
    user_id: str,
) -> GateResult:
    """Gate check before every tool execution.

    SHP.4 policy (audit-first, I-SEC11):
    - All tiers: ALLOW + audit record.
    - TIER3: additionally log at WARNING for operator visibility.
    SHP.6 enforcement: flip TIER3 to DENY after audit-period confirms safety.

    Never raises — gate failure must not block tool execution.
    """
    try:
        tier = classify_tier(tool_name, params)
    except Exception as exc:
        log.error("capability_gate: classify_tier failed: %s — defaulting TIER0", exc)
        tier = CapabilityTier.TIER0

    result = GateResult(
        allowed=True,
        tier=tier,
        reason="allowed (SHP.4 audit-first)",
        tool_name=tool_name,
        user_id=user_id,
    )

    if tier == CapabilityTier.TIER3:
        log.warning(
            "capability_gate: TIER3 tool=%s user=%s workdir=%s",
            tool_name, user_id, workdir,
        )

    _audit_action(result, workdir)
    return result

def _audit_action(result: GateResult, workdir: str) -> None:
    """Append structured audit record to hash-chained log. Best-effort — never raises."""
    try:
        from rawos.kernel import audit_chain as _ac
        _ac.append(
            "tool_call",
            {
                "tool": result.tool_name,
                "user_id": result.user_id,
                "tier": result.tier.value,
                "allowed": result.allowed,
                "reason": result.reason,
                "workdir": workdir,
            },
        )
    except Exception as exc:
        log.warning("capability_gate: audit write failed: %s", exc)
