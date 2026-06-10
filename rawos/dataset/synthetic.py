"""
rawos Synthetic Dataset Generator — Phase 8.

Uses DeepSeek to generate realistic (behavioral_context, true_goal, true_domain)
triples for each valid domain. These form the majority of the training dataset.

Design:
  - One API call per domain (batch of N examples)
  - Temperature 0.8: enough variety to avoid repetition across calls
  - Strict JSON output: validated against schema before insertion
  - Retry once on JSON parse failure (different temperature)
"""
from __future__ import annotations

import asyncio
import json
import logging

import aiohttp

from rawos.config import settings
from rawos.dataset.schema import BehavioralContext, DatasetExample, VALID_DOMAINS

log = logging.getLogger("rawos.dataset.synthetic")

_DOMAIN_CONTEXT: dict[str, str] = {
    "debugging":   "The developer is tracking down a bug or exception in their code.",
    "feature":     "The developer is implementing a new feature or capability.",
    "refactor":    "The developer is restructuring existing code for clarity or maintainability.",
    "auth":        "The developer is working on authentication, authorization, or session management.",
    "data":        "The developer is working on database schema, migrations, or data modeling.",
    "api":         "The developer is designing or implementing API endpoints.",
    "ui":          "The developer is building or fixing frontend components, layouts, or styles.",
    "performance": "The developer is profiling, optimizing, or caching to improve speed.",
    "testing":     "The developer is writing, fixing, or running tests.",
    "deployment":  "The developer is deploying, configuring, or managing production infrastructure.",
    "research":    "The developer is exploring concepts, reading papers, or preparing research output.",
    "general":     "The developer is doing general project work with mixed signals.",
}

_PROMPT_TEMPLATE = """\
You are generating ground-truth examples for a research dataset. The dataset trains \
an intent inference engine that observes developer behavioral context and infers their goal.

Generate exactly {n} examples for domain: "{domain}"
Domain description: {context}

Rules:
- behavioral_context MUST match the exact schema (see below)
- true_goal MUST be specific and actionable (not generic like "work on project")
- true_goal MUST be in English
- expected_confidence: 0.50–0.90 based on how unambiguous the signals are
- recent_activity items look like: "edit src/auth.py", "create migrations/004.sql", "run pytest"
- inferred_stack uses these values: python, typescript, javascript, go, rust, c, cpp, sql, bash, html, css, yaml, markdown, json, toml
- active_domains is a list of domain strings matching the ones below
- All examples must be distinct and realistic

Valid domains: debugging, feature, refactor, auth, data, api, ui, performance, testing, deployment, research, general

Output ONLY a valid JSON array with no markdown, no code fences, no explanation:
[
  {{
    "behavioral_context": {{
      "inferred_stack": ["python", "fastapi"],
      "active_domains": ["{domain}"],
      "recent_activity": ["edit src/main.py", "edit src/models.py", "run pytest"],
      "project_count": 2,
      "artifact_count": 5
    }},
    "true_goal": "specific actionable goal in English",
    "true_domain": "{domain}",
    "expected_confidence": 0.75,
    "notes": "brief note on what makes this characteristic of {domain}"
  }}
]
"""


async def _call_deepseek(prompt: str, temperature: float = 0.8) -> str:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{settings.deepseek_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.deepseek_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.deepseek_model_fast,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": 4096,
            },
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["choices"][0]["message"]["content"].strip()


def _parse_examples(raw: str, domain: str) -> list[DatasetExample]:
    # Strip markdown fences if DeepSeek wraps output
    content = raw.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(
            line for line in lines if not line.startswith("```")
        ).strip()

    items = json.loads(content)
    if not isinstance(items, list):
        raise ValueError(f"expected JSON array, got {type(items).__name__}")

    examples: list[DatasetExample] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            log.warning("synthetic[%s][%d] not a dict, skipping", domain, i)
            continue

        raw_domain = str(item.get("true_domain", domain)).strip().lower()
        if raw_domain not in VALID_DOMAINS:
            log.warning("synthetic[%s][%d] bad domain %r, forcing to %s", domain, i, raw_domain, domain)
            raw_domain = domain

        ctx_data = item.get("behavioral_context", {})
        if not isinstance(ctx_data, dict):
            ctx_data = {}

        ctx = BehavioralContext.from_dict(ctx_data)

        goal = str(item.get("true_goal", "")).strip()
        conf_raw = item.get("expected_confidence")
        try:
            conf = float(conf_raw) if conf_raw is not None else None
        except (TypeError, ValueError):
            conf = None

        ex = DatasetExample(
            source="synthetic",
            behavioral_context=ctx,
            true_goal=goal,
            true_domain=raw_domain,
            expected_confidence=conf,
            quality_score=4,  # synthetic labels are clean by construction
            notes=str(item.get("notes", ""))[:200],
        )
        examples.append(ex)

    return examples


async def generate_synthetic(domain: str, n: int = 8) -> list[DatasetExample]:
    """
    Generate n synthetic DatasetExamples for the given domain.
    Retries once with lower temperature on JSON parse failure.
    Returns validated examples only (invalid ones logged and skipped).
    """
    if domain not in VALID_DOMAINS:
        raise ValueError(f"unknown domain: {domain!r}")

    prompt = _PROMPT_TEMPLATE.format(
        n=n,
        domain=domain,
        context=_DOMAIN_CONTEXT.get(domain, "general developer work"),
    )

    raw: str = ""
    for attempt, temp in enumerate([0.8, 0.4], start=1):
        try:
            raw = await _call_deepseek(prompt, temperature=temp)
            break
        except Exception as exc:
            log.warning("synthetic[%s] DeepSeek call attempt %d failed: %s", domain, attempt, exc)
            if attempt == 2:
                return []
            await asyncio.sleep(3)

    if not raw:
        return []

    try:
        candidates = _parse_examples(raw, domain)
    except (json.JSONDecodeError, ValueError) as exc:
        log.error("synthetic[%s] JSON parse failed: %s | raw[:200]=%s", domain, exc, raw[:200])
        # Retry once with more explicit prompt at lower temperature
        prompt_strict = prompt + "\n\nIMPORTANT: Output ONLY the raw JSON array. No text before or after."
        try:
            raw2 = await _call_deepseek(prompt_strict, temperature=0.3)
            candidates = _parse_examples(raw2, domain)
        except Exception as exc2:
            log.error("synthetic[%s] retry parse also failed: %s", domain, exc2)
            return []

    valid: list[DatasetExample] = []
    for ex in candidates:
        errs = ex.validate()
        if errs:
            log.warning("synthetic[%s] example invalid: %s | goal=%s", domain, errs, ex.true_goal[:60])
        else:
            valid.append(ex)

    log.info("synthetic[%s]: %d/%d examples valid", domain, len(valid), len(candidates))
    return valid
