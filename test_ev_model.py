"""Tests for ev_model (pytest style, stdlib only)."""

import json
import random

import ev_model


def _make_separable(n=400, seed=11):
    """Linearly separable 2D data: label 1 when x0 + x1 > 0 (plus noise)."""
    rng = random.Random(seed)
    rows, labels = [], []
    for _ in range(n):
        x0 = rng.uniform(-3, 3)
        x1 = rng.uniform(-3, 3)
        noise = rng.gauss(0, 0.25)
        label = 1 if (x0 + x1 + noise) > 0 else 0
        rows.append([x0, x1])
        labels.append(label)
    return rows, labels


def test_separable_accuracy_and_auc():
    rows, labels = _make_separable()
    tr_r, tr_l, va_r, va_l = ev_model.train_val_split(rows, labels,
                                                      val_frac=0.3, seed=7)
    model = ev_model.train_logistic(tr_r, tr_l, l2=1.0, iters=400, lr=0.3,
                                    feature_names=["x0", "x1"])
    metrics = ev_model.evaluate(model, va_r, va_l)
    assert metrics["accuracy"] >= 0.9, metrics
    assert metrics["auc"] >= 0.9, metrics


def test_predict_proba_in_range():
    rows, labels = _make_separable(n=120, seed=3)
    model = ev_model.train_logistic(rows, labels)
    for x in [[-5, -5], [5, 5], [0, 0], [100, -100]]:
        p = ev_model.predict_proba(model, x)
        assert 0.0 <= p <= 1.0


def test_json_round_trip():
    rows, labels = _make_separable(n=150, seed=5)
    model = ev_model.train_logistic(rows, labels, feature_names=["a", "b"])
    restored = json.loads(json.dumps(model))
    x = [1.3, -0.7]
    assert ev_model.predict_proba(model, x) == ev_model.predict_proba(restored, x)


def test_empty_input():
    model = ev_model.train_logistic([], [])
    # Must not crash and should predict roughly the base rate (0.5 here).
    p = ev_model.predict_proba(model, [1.0, 2.0, 3.0])
    assert abs(p - 0.5) < 1e-6
