"""
rawos Dataset Schema — Phase 8.

DatasetExample is the ground-truth record used to train and evaluate
the intent inference engine (Phase 9). behavioral_context mirrors the
exact dict that intent_engine._rule_infer() and _llm_infer() receive
from get_user_model().
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

VALID_DOMAINS: frozenset[str] = frozenset({
    "debugging", "feature", "refactor", "auth",
    "data", "api", "ui", "performance",
    "testing", "deployment", "research", "general",
})


@dataclass
class BehavioralContext:
    inferred_stack: list[str] = field(default_factory=list)
    active_domains: list[str] = field(default_factory=list)
    recent_activity: list[str] = field(default_factory=list)
    project_count: int = 1
    artifact_count: int = 0

    def to_dict(self) -> dict:
        return {
            "inferred_stack": self.inferred_stack,
            "active_domains": self.active_domains,
            "recent_activity": self.recent_activity,
            "project_count": self.project_count,
            "artifact_count": self.artifact_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BehavioralContext":
        return cls(
            inferred_stack=d.get("inferred_stack") or [],
            active_domains=d.get("active_domains") or [],
            recent_activity=d.get("recent_activity") or [],
            project_count=int(d.get("project_count") or 1),
            artifact_count=int(d.get("artifact_count") or 0),
        )


@dataclass
class DatasetExample:
    source: str                                # 'extracted' | 'synthetic' | 'rated'
    behavioral_context: BehavioralContext
    true_goal: str
    true_domain: str
    id: str = ""
    expected_confidence: float | None = None
    quality_score: int = 3
    created_at: int = 0
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = int(time.time())

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.source not in ("extracted", "synthetic", "rated"):
            errors.append(f"invalid source: {self.source!r}")
        if self.true_domain not in VALID_DOMAINS:
            errors.append(f"invalid domain: {self.true_domain!r} — must be one of {sorted(VALID_DOMAINS)}")
        if not self.true_goal.strip():
            errors.append("true_goal is empty")
        if len(self.true_goal) < 10:
            errors.append(f"true_goal too short ({len(self.true_goal)} chars)")
        if self.expected_confidence is not None:
            if not (0.0 <= self.expected_confidence <= 1.0):
                errors.append(f"expected_confidence {self.expected_confidence} out of [0, 1]")
        if not self.behavioral_context.inferred_stack and not self.behavioral_context.active_domains:
            errors.append("behavioral_context has no stack and no domains — underspecified")
        return errors

    def to_row(self) -> tuple:
        return (
            self.source,
            json.dumps(self.behavioral_context.to_dict()),
            self.true_goal,
            self.true_domain,
            self.expected_confidence,
            self.quality_score,
            self.created_at,
            self.notes or "",
        )

    @classmethod
    def from_row(cls, row: dict) -> "DatasetExample":
        return cls(
            id=row["id"],
            source=row["source"],
            behavioral_context=BehavioralContext.from_dict(
                json.loads(row["behavioral_context"] or "{}")
            ),
            true_goal=row["true_goal"],
            true_domain=row["true_domain"],
            expected_confidence=row["expected_confidence"],
            quality_score=row["quality_score"] or 3,
            created_at=row["created_at"] or 0,
            notes=row["notes"] or "",
        )
