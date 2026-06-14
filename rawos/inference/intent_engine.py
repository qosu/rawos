"""
rawos Intent Inference Engine.

Two-layer inference:
  Layer 1 (fast, free): rule-based heuristics from user model
  Layer 2 (accurate):   LLM-based structured inference from semantic context

Output: InferredIntent — goal text, confidence [0,1], domain, suggested_actions.

Results are cached per-user for 90s to avoid LLM spam.
Results are written back to user_model.inferred_goal + goal_confidence.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from rawos.kernel.agent_loop import _log_usage
from dataclasses import dataclass, field
from typing import Any

import rawos.db as db
from rawos.config import settings
from rawos.context.user_model import get_user_model, rebuild_user_model
from rawos.kernel import llm_client

log = logging.getLogger("rawos.inference.intent_engine")

_CACHE_TTL_S = 90.0

# Simple in-process cache: {user_id: (expires_at, InferredIntent)}
_cache: dict[str, tuple[float, "InferredIntent"]] = {}


@dataclass
class InferredIntent:
    goal: str
    confidence: float          # 0.0 – 1.0
    domain: str                # e.g. "debugging", "feature", "research"
    suggested_actions: list[str] = field(default_factory=list)
    source: str = "rule"       # "rule" | "classifier" | "llm"
    inferred_at: float = field(default_factory=time.time)


_EMPTY = InferredIntent(goal="", confidence=0.0, domain="unknown")

# ---------------------------------------------------------------------------
# Classifier layer (Phase 9) — fast ML domain prediction, no API call
# ---------------------------------------------------------------------------

_CLASSIFIER = None  # IntentClassifier | None, loaded at startup


def load_classifier() -> None:
    """Load trained classifier from disk into module-level _CLASSIFIER."""
    global _CLASSIFIER
    try:
        from rawos.inference.classifier import IntentClassifier
        clf = IntentClassifier.load()
        if clf is not None:
            _CLASSIFIER = clf
            log.info("intent classifier loaded: type=%s cv_f1=%.3f",
                     clf.model_type, clf.cv_f1_mean)
        else:
            log.info("no trained classifier found; falling back to rule + LLM")
    except Exception as exc:
        log.warning("classifier load failed (non-fatal): %s", exc)


_DOMAIN_GOAL_TEMPLATES: dict[str, str] = {
    "debugging":   "debug {stack} issue",
    "feature":     "implement new feature in {stack}",
    "refactor":    "refactor {stack} codebase",
    "auth":        "work on authentication in {stack}",
    "data":        "work on database model and migrations",
    "api":         "build/fix API endpoints in {stack}",
    "ui":          "implement UI components and styles",
    "performance": "optimize {stack} performance",
    "testing":     "write and fix tests in {stack}",
    "deployment":  "deploy and configure {stack} service",
    "research":    "research and document findings",
    "general":     "work on {stack} project",
}


def _classifier_infer(model: dict) -> "InferredIntent | None":
    """
    ML classifier inference. Returns InferredIntent if classifier is loaded
    and confidence >= 0.50. Goal text from domain template (fast path).
    Returns None if classifier not loaded or confidence too low.
    """
    if _CLASSIFIER is None:
        return None
    try:
        domain, confidence = _CLASSIFIER.predict(model)
        if confidence < 0.50:
            return None
        stack = model.get("inferred_stack") or []
        stack_str = " + ".join(stack[:2]) if stack else "project"
        goal = _DOMAIN_GOAL_TEMPLATES.get(domain, "work on {stack}").format(stack=stack_str)
        return InferredIntent(
            goal=goal,
            confidence=confidence,
            domain=domain,
            suggested_actions=[f"continue {domain} work", "review recent changes"],
            source="classifier",
        )
    except Exception as exc:
        log.warning("classifier inference error (non-fatal): %s", exc)
        return None



def _rule_infer(model: dict[str, Any]) -> InferredIntent | None:
    """
    Fast rule-based inference. Returns InferredIntent only when confidence >= 0.5.
    Uses domain tags + recent activity patterns.
    """
    domains: list[str] = model.get("active_domains", [])
    stack:   list[str] = model.get("inferred_stack", [])
    recent:  list[dict] = model.get("recent_activity", [])

    if not domains and not recent:
        return None

    # Count intent events in last N
    intent_messages = [
        r["preview"] for r in recent
        if r.get("type") == "intent_sent" and r.get("preview")
    ]

    primary_domain = domains[0] if domains else "general"
    stack_str = " + ".join(stack[:3]) if stack else "unknown stack"

    # High-confidence patterns
    if len(intent_messages) >= 3:
        # Multiple intents → active session → describe what they're doing
        last_intent = intent_messages[0]
        conf = min(0.5 + len(intent_messages) * 0.05, 0.85)
        return InferredIntent(
            goal=f"{primary_domain} work on {stack_str} project",
            confidence=conf,
            domain=primary_domain,
            suggested_actions=[f"continue {primary_domain}", "summarize progress"],
            source="rule",
        )

    # File write pattern
    file_writes = [r for r in recent if r.get("type") == "file_write"]
    if len(file_writes) >= 5 and stack:
        return InferredIntent(
            goal=f"active development in {stack_str}",
            confidence=0.55,
            domain=primary_domain,
            suggested_actions=["analyze recent changes", "run tests", "check for issues"],
            source="rule",
        )

    return None


_LLM_SYSTEM = """\
You are rawos intent inference engine. Given a user's recent behavioral context, \
infer their primary goal with precision and confidence.

Reply ONLY with valid JSON — no prose, no code blocks. Schema:
{
  "goal": "<concise description of what user is trying to accomplish, ≤ 80 chars>",
  "confidence": <float 0.0–1.0>,
  "domain": "<one of: debugging|feature|refactor|auth|data|api|ui|performance|testing|deployment|research|general>",
  "suggested_actions": ["<action 1>", "<action 2>"]
}

Rules:
- confidence < 0.4 if context is sparse or ambiguous
- confidence 0.7+ only if multiple signals strongly agree
- goal must be specific and actionable, not generic ("write code" is bad, "debug jwt refresh token expiry" is good)
- If no clear signal, return confidence 0.0 and empty goal."""


async def _llm_infer(model: dict[str, Any]) -> InferredIntent:
    """LLM-based inference. Calls configured LLM provider with user model summary."""
    stack = model.get("inferred_stack", [])
    domains = model.get("active_domains", [])
    recent = model.get("recent_activity", [])

    # Compress recent activity into a readable summary for the prompt
    activity_lines: list[str] = []
    for ev in recent[:15]:
        etype = ev.get("type", "")
        if etype == "intent_sent":
            activity_lines.append(f'  intent: "{ev.get("preview", "")}"')
        elif etype == "file_write":
            activity_lines.append(f'  wrote: {ev.get("file", "")} ({ev.get("ext", "")})')
        elif etype == "artifact_created":
            activity_lines.append(f'  artifact: {ev.get("name", "")}')

    user_context = (
        f"Tech stack: {', '.join(stack) or 'unknown'}\n"
        f"Active domains: {', '.join(domains) or 'none detected'}\n"
        f"Recent activity (newest first):\n" + "\n".join(activity_lines or ["  (none)"])
    )

    try:
        content, usage = await llm_client.complete(
            [
                {"role": "system", "content": _LLM_SYSTEM},
                {"role": "user", "content": user_context},
            ],
            model=settings.llm_summarizer_model,
            max_tokens=500,
            temperature=0.1,
        )
        _log_usage(settings.llm_summarizer_model, usage)
        content = content.strip()
        # Strip code fences if model adds them despite instructions
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        parsed = json.loads(content)
        return InferredIntent(
            goal=str(parsed.get("goal", ""))[:80],
            confidence=float(parsed.get("confidence", 0.0)),
            domain=str(parsed.get("domain", "general")),
            suggested_actions=[str(a) for a in parsed.get("suggested_actions", [])[:3]],
            source="llm",
        )
    except Exception:
        log.exception("LLM intent inference failed")
        return _EMPTY


def _write_back(user_id: str, intent: InferredIntent) -> None:
    now = int(time.time())
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO user_model (user_id, inferred_goal, goal_confidence, goal_domain, goal_updated_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 inferred_goal    = excluded.inferred_goal,
                 goal_confidence  = excluded.goal_confidence,
                 goal_domain      = excluded.goal_domain,
                 goal_updated_at  = excluded.goal_updated_at,
                 updated_at       = excluded.updated_at""",
            (user_id, intent.goal, intent.confidence, intent.domain, now, now),
        )


async def infer_intent(user_id: str, force_llm: bool = False) -> InferredIntent:
    """
    Main entry point. Returns cached result if fresh enough.
    First tries rule-based; escalates to LLM if confidence < 0.6 or force_llm.
    """
    now = time.time()
    cached = _cache.get(user_id)
    if cached and cached[0] > now and not force_llm:
        return cached[1]

    # Rebuild user model from last hour of events
    model = await asyncio.get_event_loop().run_in_executor(
        None, rebuild_user_model, user_id
    )

    # Layer 1: rule-based (fast heuristic)
    rule_result = _rule_infer(model)
    if rule_result and rule_result.confidence >= 0.6 and not force_llm:
        result = rule_result
    else:
        # Layer 2: classifier (fast ML, no API, Phase 9)
        clf_result = _classifier_infer(model)
        if clf_result and clf_result.confidence >= 0.65 and not force_llm:
            result = clf_result
        else:
            # Layer 3: LLM (accurate goal text, API call)
            result = await _llm_infer(model)
            # If LLM also fails, fall back to best available
            if result.confidence < 0.1:
                result = clf_result or rule_result or _EMPTY

    _cache[user_id] = (now + _CACHE_TTL_S, result)
    await asyncio.get_event_loop().run_in_executor(None, _write_back, user_id, result)
    return result
