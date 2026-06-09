"""
rawos Intent Inference Benchmark — Phase 9.

Evaluates three inference strategies against the labeled_examples ground truth:
  1. rule  — heuristic: return active_domains[0] (current production baseline)
  2. classifier — trained sklearn model (fast, no API)
  3. llm   — DeepSeek async inference (sampled subset, too expensive for full set)

Metrics: precision, recall, F1 per domain + macro average (scikit-learn).
Results saved to /root/rawos/data/benchmark_results.json.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np

from rawos.inference.features import (
    DOMAIN_ORDER, domain_to_label, label_to_domain,
)

log = logging.getLogger("rawos.inference.benchmark")

_RESULTS_PATH = Path("/root/rawos/data/benchmark_results.json")


# ---------------------------------------------------------------------------
# Rule baseline
# ---------------------------------------------------------------------------

def _rule_predict(context: dict) -> tuple[str, float]:
    """
    Simulate production rule_infer domain decision on a behavioral_context dict.
    Returns (predicted_domain, confidence).
    """
    domains: list[str] = context.get("active_domains") or []
    stack: list[str]   = context.get("inferred_stack")  or []
    recent: list       = context.get("recent_activity")  or []

    if not domains and not recent:
        return "general", 0.25

    primary = domains[0] if domains else "general"
    # Confidence mirrors rule_infer heuristic
    conf = 0.55 if (stack and domains) else 0.45
    return primary, conf


# ---------------------------------------------------------------------------
# Metrics helpers
# ---------------------------------------------------------------------------

def _compute_classification_metrics(
    y_true: list[str], y_pred: list[str],
) -> dict:
    """
    Compute precision, recall, F1 (macro + per-domain) from string labels.
    No sklearn dependency needed for this simple case.
    """
    from sklearn.metrics import (
        classification_report, f1_score, precision_score, recall_score,
    )
    import numpy as np

    label_set = sorted(set(y_true) | set(y_pred))
    report = classification_report(
        y_true, y_pred, labels=label_set, output_dict=True, zero_division=0
    )

    per_domain = {
        domain: {
            "precision": round(report.get(domain, {}).get("precision", 0.0), 4),
            "recall":    round(report.get(domain, {}).get("recall",    0.0), 4),
            "f1":        round(report.get(domain, {}).get("f1-score",  0.0), 4),
            "support":   int(  report.get(domain, {}).get("support",   0  )),
        }
        for domain in DOMAIN_ORDER
        if domain in report
    }

    return {
        "macro_precision": round(float(precision_score(y_true, y_pred, average="macro", zero_division=0, labels=label_set)), 4),
        "macro_recall":    round(float(recall_score(   y_true, y_pred, average="macro", zero_division=0, labels=label_set)), 4),
        "macro_f1":        round(float(f1_score(       y_true, y_pred, average="macro", zero_division=0, labels=label_set)), 4),
        "accuracy":        round(sum(p == t for p, t in zip(y_pred, y_true)) / len(y_true), 4),
        "per_domain":      per_domain,
        "n_examples":      len(y_true),
    }


# ---------------------------------------------------------------------------
# Benchmark runners
# ---------------------------------------------------------------------------

def run_rule_benchmark(examples: list[dict]) -> dict:
    """Evaluate rule baseline on all labeled examples."""
    y_true = [ex["true_domain"] for ex in examples]
    y_pred = [_rule_predict(ex["behavioral_context"])[0] for ex in examples]
    metrics = _compute_classification_metrics(y_true, y_pred)
    return {"strategy": "rule", **metrics}


def run_classifier_benchmark(
    examples: list[dict],
    classifier=None,
) -> dict:
    """
    Evaluate classifier on all examples using 5-fold stratified CV.
    If classifier is None, loads from disk (must exist).
    """
    import json as _json
    from sklearn.model_selection import StratifiedKFold, cross_val_predict
    from rawos.inference.features import build_feature_matrix, domain_to_label, label_to_domain

    contexts = [ex["behavioral_context"] for ex in examples]
    y_true_str = [ex["true_domain"] for ex in examples]
    y_true_int = np.array([domain_to_label(d) for d in y_true_str], dtype=np.int32)

    X = build_feature_matrix(contexts)

    if classifier is None:
        from rawos.inference.classifier import IntentClassifier
        classifier = IntentClassifier.load()
        if classifier is None:
            raise RuntimeError("no trained classifier found — run rawos classifier train first")

    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    y_pred_int = cross_val_predict(classifier.model, X, y_true_int, cv=cv)
    y_pred_str = [label_to_domain(int(i)) for i in y_pred_int]

    metrics = _compute_classification_metrics(y_true_str, y_pred_str)
    return {
        "strategy": f"classifier_{classifier.model_type}",
        "cv_f1_mean": round(classifier.cv_f1_mean, 4),
        "cv_f1_std":  round(classifier.cv_f1_std,  4),
        **metrics,
    }


async def run_llm_benchmark(examples: list[dict], n_sample: int = 30) -> dict:
    """
    Evaluate LLM domain prediction on a random sample.
    Calls DeepSeek once per example — expensive. Default: 30 examples.
    """
    import random
    import rawos.inference.intent_engine as engine

    rng = random.Random(42)
    sample = rng.sample(examples, min(n_sample, len(examples)))

    y_true: list[str] = []
    y_pred: list[str] = []
    latencies: list[float] = []
    errors = 0

    for ex in sample:
        ctx = ex["behavioral_context"]
        t0 = time.time()
        try:
            result = await engine._llm_infer(ctx)
            y_pred.append(result.domain if result.domain in DOMAIN_ORDER else "general")
            y_true.append(ex["true_domain"])
            latencies.append(round(time.time() - t0, 3))
        except Exception as exc:
            log.warning("llm_benchmark: inference failed: %s", exc)
            errors += 1
            # Still count as a wrong prediction (general) for honest reporting
            y_pred.append("general")
            y_true.append(ex["true_domain"])
            latencies.append(round(time.time() - t0, 3))

    metrics = _compute_classification_metrics(y_true, y_pred)
    return {
        "strategy": "llm",
        "sample_size": len(sample),
        "errors": errors,
        "avg_latency_s": round(float(np.mean(latencies)), 3),
        **metrics,
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run_full_benchmark(llm_sample: int = 0) -> dict:
    """
    Run all benchmarks, save results to disk, return summary.
    llm_sample=0 skips LLM benchmark (expensive).
    """
    import rawos.db as db
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT behavioral_context, true_goal, true_domain FROM labeled_examples"
        ).fetchall()

    examples = [
        {
            "behavioral_context": json.loads(r["behavioral_context"] or "{}"),
            "true_goal":          r["true_goal"],
            "true_domain":        r["true_domain"],
        }
        for r in rows
    ]

    if not examples:
        return {"error": "no labeled examples — run rawos dataset build first"}

    results: dict = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "dataset_size": len(examples),
        "strategies": {},
    }

    log.info("benchmark: %d examples", len(examples))

    # Rule baseline
    rule_metrics = run_rule_benchmark(examples)
    results["strategies"]["rule"] = rule_metrics
    log.info("rule:       macro_f1=%.4f accuracy=%.4f",
             rule_metrics["macro_f1"], rule_metrics["accuracy"])

    # Classifier (CV)
    try:
        from rawos.inference.classifier import IntentClassifier
        clf = IntentClassifier.load()
        if clf is None:
            results["strategies"]["classifier"] = {"error": "model not trained"}
        else:
            clf_metrics = run_classifier_benchmark(examples, classifier=clf)
            results["strategies"]["classifier"] = clf_metrics
            log.info("classifier: macro_f1=%.4f accuracy=%.4f",
                     clf_metrics["macro_f1"], clf_metrics["accuracy"])
    except Exception as exc:
        log.error("classifier benchmark failed: %s", exc)
        results["strategies"]["classifier"] = {"error": str(exc)}

    # LLM (optional sample)
    if llm_sample > 0:
        try:
            llm_metrics = await run_llm_benchmark(examples, n_sample=llm_sample)
            results["strategies"]["llm"] = llm_metrics
            log.info("llm:        macro_f1=%.4f n=%d latency=%.2fs",
                     llm_metrics["macro_f1"], llm_metrics["sample_size"],
                     llm_metrics["avg_latency_s"])
        except Exception as exc:
            log.error("llm benchmark failed: %s", exc)
            results["strategies"]["llm"] = {"error": str(exc)}

    # Summary: rank by macro F1
    ranked = sorted(
        [(name, s.get("macro_f1", 0.0)) for name, s in results["strategies"].items()
         if "error" not in s],
        key=lambda x: -x[1],
    )
    results["ranking"] = [{"strategy": name, "macro_f1": f1} for name, f1 in ranked]
    results["best_strategy"] = ranked[0][0] if ranked else "none"

    # Persist
    _RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_RESULTS_PATH, "w") as fh:
        json.dump(results, fh, indent=2)
    log.info("benchmark results saved to %s", _RESULTS_PATH)

    return results


def load_cached_results() -> dict | None:
    if not _RESULTS_PATH.exists():
        return None
    try:
        with open(_RESULTS_PATH) as fh:
            return json.load(fh)
    except Exception:
        return None
