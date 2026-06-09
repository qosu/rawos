"""
rawos Intent Classifier — Phase 9.

Trains 3 sklearn models (LR, RF, MLP) with 5-fold stratified CV on the
labeled_examples dataset, selects the one with best macro F1, saves to disk.

Prediction path: context dict → 70-dim feature vector → domain + confidence.
This replaces the rule-based layer in the inference engine.
"""
from __future__ import annotations

import json
import logging
import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from rawos.inference.features import (
    DOMAIN_ORDER, FEATURE_DIM,
    build_feature_matrix, domain_to_label, extract_feature_vector, label_to_domain,
)

log = logging.getLogger("rawos.inference.classifier")

_MODEL_PATH = Path("/root/rawos/data/intent_classifier.pkl")


@dataclass
class IntentClassifier:
    """Wrapper around a trained sklearn pipeline for domain prediction."""
    model: Any                         # sklearn Pipeline or estimator
    model_type: str = ""               # "lr" | "rf" | "mlp"
    feature_version: str = "v1"
    cv_f1_mean: float = 0.0
    cv_f1_std: float = 0.0
    trained_at: float = field(default_factory=time.time)
    training_size: int = 0
    cv_results: dict = field(default_factory=dict)

    def predict(self, context: dict) -> tuple[str, float]:
        """
        Predict domain and confidence from a behavioral_context dict.
        Returns ("general", 0.0) on any error — never raises.

        Confidence is margin-based: (top_proba - second_proba) + 0.45,
        capped at 0.95. This calibrates RF 12-class output (raw max ~0.35)
        to the engine confidence scale where 0.65 triggers proactive action.
        margin=0.05 -> conf=0.50; margin=0.20 -> conf=0.65.
        """
        try:
            fv = extract_feature_vector(context).reshape(1, -1)
            proba = self.model.predict_proba(fv)[0]
            label_idx = int(np.argmax(proba))
            sorted_p = sorted(proba, reverse=True)
            top = float(sorted_p[0])
            second = float(sorted_p[1]) if len(sorted_p) > 1 else 0.0
            confidence = min(0.45 + (top - second), 0.95)
            return label_to_domain(label_idx), confidence
        except Exception as exc:
            log.error("classifier predict error: %s", exc)
            return "general", 0.0

    def save(self, path: Path = _MODEL_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            pickle.dump(self, fh, protocol=5)
        log.info("classifier saved to %s (type=%s cv_f1=%.3f)", path, self.model_type, self.cv_f1_mean)

    @classmethod
    def load(cls, path: Path = _MODEL_PATH) -> "IntentClassifier | None":
        if not path.exists():
            return None
        try:
            with open(path, "rb") as fh:
                obj = pickle.load(fh)
            log.info("classifier loaded from %s (type=%s cv_f1=%.3f)",
                     path, obj.model_type, obj.cv_f1_mean)
            return obj
        except Exception as exc:
            log.error("failed to load classifier from %s: %s", path, exc)
            return None


def _load_dataset() -> tuple[np.ndarray, np.ndarray, list[str]]:
    """
    Load all labeled_examples from DB.
    Returns (X, y, goals) where X is (N, 70), y is (N,) int labels.
    """
    import rawos.db as db
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT behavioral_context, true_goal, true_domain FROM labeled_examples"
        ).fetchall()

    contexts: list[dict] = []
    goals: list[str] = []
    labels: list[int] = []

    for row in rows:
        ctx = json.loads(row["behavioral_context"] or "{}")
        contexts.append(ctx)
        goals.append(row["true_goal"])
        labels.append(domain_to_label(row["true_domain"]))

    X = build_feature_matrix(contexts)
    y = np.array(labels, dtype=np.int32)
    return X, y, goals


def train(save: bool = True) -> IntentClassifier:
    """
    Train 3 models with 5-fold stratified CV, select best macro F1.
    Refits best model on full dataset, saves to disk if save=True.
    """
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import f1_score
    from sklearn.model_selection import StratifiedKFold, cross_val_score
    from sklearn.neural_network import MLPClassifier
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    X, y, _ = _load_dataset()
    n_samples = len(y)
    n_classes = len(set(y.tolist()))
    log.info("training on %d examples, %d classes, %d features", n_samples, n_classes, FEATURE_DIM)

    if n_samples < 20:
        raise RuntimeError(f"dataset too small ({n_samples} examples); need ≥20 to train")

    candidate_models: dict[str, Any] = {
        "lr": Pipeline([
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(
                C=1.0, max_iter=1000, random_state=42,
                class_weight="balanced", solver="lbfgs",
            )),
        ]),
        "rf": RandomForestClassifier(
            n_estimators=200, max_depth=8, min_samples_leaf=2,
            random_state=42, class_weight="balanced",
        ),
        "mlp": Pipeline([
            ("scaler", StandardScaler()),
            ("clf", MLPClassifier(
                hidden_layer_sizes=(64, 32), activation="relu",
                max_iter=500, random_state=42,
                early_stopping=True, validation_fraction=0.15,
                learning_rate_init=0.001,
            )),
        ]),
    }

    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cv_results: dict[str, dict] = {}
    best_f1 = -1.0
    best_name = ""
    best_pipeline = None

    for name, model in candidate_models.items():
        scores = cross_val_score(model, X, y, cv=cv, scoring="f1_macro", n_jobs=-1)
        cv_results[name] = {
            "cv_f1_mean": float(scores.mean()),
            "cv_f1_std":  float(scores.std()),
            "scores":     [round(s, 4) for s in scores.tolist()],
        }
        log.info("  [%s] macro F1: %.4f ± %.4f", name, scores.mean(), scores.std())
        if scores.mean() > best_f1:
            best_f1  = scores.mean()
            best_name = name
            best_pipeline = candidate_models[name]

    # Refit best model on full dataset
    best_pipeline.fit(X, y)

    # Per-class F1 on full training set (optimistic — useful for diagnostics only)
    y_pred = best_pipeline.predict(X)
    per_class_f1 = {
        DOMAIN_ORDER[i]: round(float(f1_score(y, y_pred, labels=[i], average="macro", zero_division=0)), 4)
        for i in range(len(DOMAIN_ORDER))
        if i in y or i in y_pred
    }

    clf = IntentClassifier(
        model=best_pipeline,
        model_type=best_name,
        cv_f1_mean=best_f1,
        cv_f1_std=cv_results[best_name]["cv_f1_std"],
        trained_at=time.time(),
        training_size=n_samples,
        cv_results=cv_results,
    )
    clf.cv_results["_best"] = best_name
    clf.cv_results["_per_class_f1_train"] = per_class_f1

    if save:
        clf.save()

    log.info("best model: %s (macro F1=%.4f)", best_name, best_f1)
    return clf
