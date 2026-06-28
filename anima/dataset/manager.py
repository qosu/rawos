"""
anima Dataset Manager — Phase 8.

Orchestrates dataset construction and provides query/export utilities.
build() is the primary entry point: extract real examples from tg-claude,
generate synthetic examples via DeepSeek, persist all to labeled_examples.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

import anima.db as db
from anima.dataset.schema import DatasetExample, VALID_DOMAINS

log = logging.getLogger("anima.dataset.manager")


def save_example(ex: DatasetExample) -> str:
    """Persist one example. Returns the inserted id."""
    with db._conn() as conn:
        row = conn.execute(
            """INSERT INTO labeled_examples
               (source, behavioral_context, true_goal, true_domain,
                expected_confidence, quality_score, created_at, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               RETURNING id""",
            ex.to_row(),
        ).fetchone()
    return row["id"]


def stats() -> dict:
    """Return dataset statistics."""
    with db._conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM labeled_examples").fetchone()[0]
        by_source = {
            row["source"]: row["cnt"]
            for row in conn.execute(
                "SELECT source, COUNT(*) as cnt FROM labeled_examples GROUP BY source"
            ).fetchall()
        }
        by_domain = {
            row["true_domain"]: row["cnt"]
            for row in conn.execute(
                "SELECT true_domain, COUNT(*) as cnt FROM labeled_examples GROUP BY true_domain ORDER BY cnt DESC"
            ).fetchall()
        }
        avg_confidence = conn.execute(
            "SELECT AVG(expected_confidence) FROM labeled_examples WHERE expected_confidence IS NOT NULL"
        ).fetchone()[0]
        avg_quality = conn.execute(
            "SELECT AVG(quality_score) FROM labeled_examples"
        ).fetchone()[0]

    domain_coverage = len(by_domain)
    domains_missing = sorted(VALID_DOMAINS - set(by_domain.keys()))

    return {
        "total": total,
        "by_source": by_source,
        "by_domain": by_domain,
        "domain_coverage": f"{domain_coverage}/{len(VALID_DOMAINS)}",
        "domains_missing": domains_missing,
        "avg_confidence": round(avg_confidence or 0.0, 3),
        "avg_quality": round(avg_quality or 0.0, 2),
    }


def list_examples(
    domain: str | None = None,
    source: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[DatasetExample]:
    conditions = []
    params: list = []
    if domain:
        conditions.append("true_domain = ?")
        params.append(domain)
    if source:
        conditions.append("source = ?")
        params.append(source)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params += [limit, offset]

    with db._conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM labeled_examples {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()

    return [DatasetExample.from_row(dict(r)) for r in rows]


def export_jsonl(path: str | None = None) -> tuple[str, int]:
    """
    Export all examples as JSON lines.
    Returns (file_path, count).
    """
    if not path:
        path = f"/tmp/anima_dataset_{int(time.time())}.jsonl"

    examples = list_examples(limit=10000)
    with open(path, "w") as fh:
        for ex in examples:
            record = {
                "id": ex.id,
                "source": ex.source,
                "behavioral_context": ex.behavioral_context.to_dict(),
                "true_goal": ex.true_goal,
                "true_domain": ex.true_domain,
                "expected_confidence": ex.expected_confidence,
                "quality_score": ex.quality_score,
                "notes": ex.notes,
            }
            fh.write(json.dumps(record) + "\n")

    return path, len(examples)


async def build(
    *,
    extract: bool = True,
    synthetic_per_domain: int = 8,
    inter_domain_delay: float = 1.5,
) -> dict:
    """
    Build the dataset from all sources.

    extract=True:  parse tg-claude sessions.db for silver-labeled real examples
    synthetic_per_domain:  number of DeepSeek-generated examples per domain

    Returns a summary dict.
    """
    summary: dict = {
        "extracted": 0,
        "synthetic": 0,
        "errors": [],
        "skipped_invalid": 0,
        "total": 0,
    }

    # --- Phase A: extract from tg-claude ---
    if extract:
        from anima.dataset.extractor import extract_from_tg_claude
        log.info("extracting from tg-claude...")
        examples = extract_from_tg_claude()
        for ex in examples:
            try:
                save_example(ex)
                summary["extracted"] += 1
            except Exception as exc:
                # Duplicate goal text causes UNIQUE constraint violation — silently skip
                if "UNIQUE" in str(exc).upper():
                    log.debug("skipping duplicate extracted example: %s", ex.true_goal[:60])
                else:
                    err = f"extract save error: {exc}"
                    log.warning(err)
                    summary["errors"].append(err)

    # --- Phase B: synthetic via DeepSeek ---
    if synthetic_per_domain > 0:
        from anima.dataset.synthetic import generate_synthetic
        domains = sorted(VALID_DOMAINS)
        log.info("generating synthetic examples: %d domains × %d each", len(domains), synthetic_per_domain)

        for domain in domains:
            try:
                examples = await generate_synthetic(domain, n=synthetic_per_domain)
                saved = 0
                for ex in examples:
                    try:
                        save_example(ex)
                        saved += 1
                    except Exception as exc:
                        err = f"synthetic[{domain}] save error: {exc}"
                        log.warning(err)
                        summary["errors"].append(err)
                summary["synthetic"] += saved
                log.info("synthetic[%s]: saved %d", domain, saved)
            except Exception as exc:
                err = f"synthetic[{domain}] generation failed: {exc}"
                log.error(err)
                summary["errors"].append(err)

            await asyncio.sleep(inter_domain_delay)

    summary["total"] = summary["extracted"] + summary["synthetic"]
    log.info("build complete: extracted=%d synthetic=%d total=%d errors=%d",
             summary["extracted"], summary["synthetic"], summary["total"], len(summary["errors"]))
    return summary
