"""
rawos Intent Inference Feature Extraction — Phase 9.

Converts behavioral_context (from user_model or labeled_examples) into a
fixed-length numpy feature vector for the ML classifier.

Feature groups:
  [0:12]   domain one-hot  — active_domains presence (12 domains)
  [12:24]  stack one-hot   — inferred_stack presence (12 common stacks)
  [24:68]  activity kw     — keyword presence in recent_activity text (44 kw)
  [68:70]  scalars         — log1p(project_count), log1p(artifact_count)

Total: 70 features

recent_activity handling: supports both string items (from labeled dataset)
and dict items (from live user_model events).
"""
from __future__ import annotations

import numpy as np

# Alphabetically ordered — positional index is the class label integer
DOMAIN_ORDER: list[str] = [
    "api", "auth", "data", "debugging", "deployment",
    "feature", "general", "performance", "refactor",
    "research", "testing", "ui",
]  # 12

STACK_ORDER: list[str] = [
    "bash", "c", "cpp", "css", "go",
    "html", "javascript", "markdown", "python", "rust",
    "sql", "typescript",
]  # 12

# Discriminative keywords extracted from activity text and goal descriptions.
# Covers all 12 domains with ~3-4 keywords each.
ACTIVITY_KEYWORDS: list[str] = [
    # api
    "api", "endpoint", "route", "request", "response",
    # auth
    "auth", "login", "token", "session", "password",
    # data
    "database", "migration", "schema", "query", "table",
    # debugging
    "bug", "error", "exception", "crash", "trace",
    # deployment
    "deploy", "docker", "nginx", "server", "service",
    # feature
    "implement", "build", "create", "feature", "new",
    # general
    "general", "project", "work",
    # performance
    "optimize", "cache", "performance", "latency",
    # refactor
    "refactor", "clean", "rename", "restructure",
    # research
    "research", "paper", "publish", "doi",
    # testing
    "test", "spec", "mock", "pytest",
    # ui
    "frontend", "component", "style", "layout",
]  # 44

FEATURE_DIM: int = len(DOMAIN_ORDER) + len(STACK_ORDER) + len(ACTIVITY_KEYWORDS) + 2
# 12 + 12 + 44 + 2 = 70


def _normalize_activity(items: list) -> str:
    """
    Convert activity items to a single lowercase text blob.
    Handles both string items (dataset) and dict items (live user_model).
    """
    parts: list[str] = []
    for item in items:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            etype = item.get("type", "")
            if etype == "intent_sent":
                parts.append(f"intent {item.get('preview', '')}")
            elif etype == "file_write":
                parts.append(f"edit {item.get('file', '')} {item.get('ext', '')}")
            elif etype == "artifact_created":
                parts.append(f"artifact {item.get('name', '')}")
    return " ".join(parts).lower()


def extract_feature_vector(context: dict) -> np.ndarray:
    """
    Convert a behavioral_context dict to a 70-dim float32 feature vector.
    Safe for missing keys (defaults to zeros / 1 for project_count).
    """
    domains: list[str] = context.get("inferred_stack") and context.get("active_domains") or \
                         context.get("active_domains") or []
    # The above is clunky — just do it cleanly:
    domains = context.get("active_domains") or []
    stack: list[str] = context.get("inferred_stack") or []
    activity_text: str = _normalize_activity(context.get("recent_activity") or [])
    project_count: float = float(context.get("project_count") or 1)
    artifact_count: float = float(context.get("artifact_count") or 0)

    domain_vec = [1.0 if d in domains else 0.0 for d in DOMAIN_ORDER]
    stack_vec  = [1.0 if s in stack  else 0.0 for s in STACK_ORDER]
    kw_vec     = [1.0 if kw in activity_text else 0.0 for kw in ACTIVITY_KEYWORDS]
    scalar_vec = [float(np.log1p(project_count)), float(np.log1p(artifact_count))]

    return np.array(domain_vec + stack_vec + kw_vec + scalar_vec, dtype=np.float32)


def build_feature_matrix(contexts: list[dict]) -> np.ndarray:
    """Stack feature vectors into an (N, 70) matrix."""
    return np.vstack([extract_feature_vector(c) for c in contexts])


def domain_to_label(domain: str) -> int:
    try:
        return DOMAIN_ORDER.index(domain)
    except ValueError:
        return DOMAIN_ORDER.index("general")


def label_to_domain(label: int) -> str:
    if 0 <= label < len(DOMAIN_ORDER):
        return DOMAIN_ORDER[label]
    return "general"
