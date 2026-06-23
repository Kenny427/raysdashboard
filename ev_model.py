"""Zero-dependency binary logistic-regression classifier (stdlib only).

Predicts the probability that a flip "completes" (round-trips) from numeric
features. Models are plain dicts so they JSON-serialize cleanly into a
calibration file.

Model dict shape::

    {
        "w": [float, ...],     # weights (one per feature, on standardized space)
        "b": float,            # bias
        "mean": [float, ...],  # per-feature training mean
        "std": [float, ...],   # per-feature training std (0 -> 1.0)
        "features": [str, ...],# optional feature names
        "n": int,              # number of training rows
    }
"""

import math
import random

__all__ = [
    "train_logistic",
    "predict_proba",
    "evaluate",
    "train_val_split",
]


def _sigmoid(z: float) -> float:
    """Numerically stable logistic sigmoid."""
    # Clamp to avoid math.exp overflow on extreme inputs.
    if z >= 0:
        z = min(z, 60.0)
        return 1.0 / (1.0 + math.exp(-z))
    z = max(z, -60.0)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _standardize(x, mean, std):
    """Return standardized feature vector (x - mean) / std."""
    return [(x[j] - mean[j]) / std[j] for j in range(len(x))]


def train_logistic(rows, labels, l2: float = 1.0, iters: int = 400,
                   lr: float = 0.1, feature_names=None) -> dict:
    """Train a binary logistic-regression model via batch gradient descent.

    Features are standardized internally (per-feature mean/std computed from
    the training rows; std==0 is replaced with 1.0). L2 regularization is
    applied to the weights but not the bias. Empty input returns a model that
    predicts the base rate (or 0.5 when there are no labels).

    Returns a JSON-serializable model dict.
    """
    n = len(rows)
    if n == 0 or not rows[0]:
        base = (sum(labels) / len(labels)) if labels else 0.5
        # Encode the base rate in the bias via the logit; zero weights.
        d = len(rows[0]) if (rows and rows[0]) else 0
        base = min(max(base, 1e-6), 1 - 1e-6)
        b = math.log(base / (1.0 - base))
        return {"w": [0.0] * d, "b": b, "mean": [0.0] * d,
                "std": [1.0] * d, "features": feature_names or [], "n": n}

    d = len(rows[0])

    # Per-feature mean and std.
    mean = [0.0] * d
    for x in rows:
        for j in range(d):
            mean[j] += x[j]
    mean = [m / n for m in mean]

    var = [0.0] * d
    for x in rows:
        for j in range(d):
            diff = x[j] - mean[j]
            var[j] += diff * diff
    std = [math.sqrt(v / n) for v in var]
    std = [s if s > 1e-12 else 1.0 for s in std]

    # Standardize once up front.
    z_rows = [_standardize(x, mean, std) for x in rows]

    w = [0.0] * d
    b = 0.0
    for _ in range(iters):
        grad_w = [0.0] * d
        grad_b = 0.0
        for i in range(n):
            zi = z_rows[i]
            p = _sigmoid(sum(w[j] * zi[j] for j in range(d)) + b)
            err = p - labels[i]
            for j in range(d):
                grad_w[j] += err * zi[j]
            grad_b += err
        # Average gradient + L2 on weights only.
        for j in range(d):
            grad_w[j] = grad_w[j] / n + l2 * w[j] / n
            w[j] -= lr * grad_w[j]
        b -= lr * (grad_b / n)

    return {"w": w, "b": b, "mean": mean, "std": std,
            "features": feature_names or [], "n": n}


def predict_proba(model: dict, x: list) -> float:
    """Return P(label=1 | x) in [0, 1] using the stored standardization."""
    mean = model["mean"]
    std = model["std"]
    w = model["w"]
    b = model["b"]
    if not w:
        return _sigmoid(b)
    z = _standardize(x, mean, std)
    return _sigmoid(sum(w[j] * z[j] for j in range(len(w))) + b)


def evaluate(model: dict, rows, labels) -> dict:
    """Compute accuracy, log-loss, and AUC on a labeled dataset.

    AUC uses the rank-based (Mann-Whitney U) method. If only one class is
    present, AUC defaults to 0.5. Probabilities are clamped away from 0/1
    before computing log-loss.
    """
    n = len(rows)
    if n == 0:
        return {"n": 0, "accuracy": 0.0, "logloss": 0.0, "auc": 0.5}

    probs = [predict_proba(model, x) for x in rows]

    correct = 0
    logloss = 0.0
    eps = 1e-12
    for i in range(n):
        pred = 1 if probs[i] >= 0.5 else 0
        if pred == labels[i]:
            correct += 1
        p = min(max(probs[i], eps), 1.0 - eps)
        logloss -= labels[i] * math.log(p) + (1 - labels[i]) * math.log(1 - p)
    accuracy = correct / n
    logloss /= n

    auc = _auc(probs, labels)
    return {"n": n, "accuracy": accuracy, "logloss": logloss, "auc": auc}


def _auc(probs, labels) -> float:
    """AUC via the rank/Mann-Whitney method with tie handling."""
    n_pos = sum(1 for y in labels if y == 1)
    n_neg = len(labels) - n_pos
    if n_pos == 0 or n_neg == 0:
        return 0.5

    # Rank probabilities (average ranks for ties), ranks start at 1.
    order = sorted(range(len(probs)), key=lambda i: probs[i])
    ranks = [0.0] * len(probs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and probs[order[j + 1]] == probs[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # +1 for 1-based ranks
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1

    sum_pos_ranks = sum(ranks[i] for i in range(len(labels)) if labels[i] == 1)
    auc = (sum_pos_ranks - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
    return auc


def train_val_split(rows, labels, val_frac: float = 0.3, seed: int = 7):
    """Deterministically shuffle and split into train/val.

    Returns (train_rows, train_labels, val_rows, val_labels).
    """
    idx = list(range(len(rows)))
    random.Random(seed).shuffle(idx)
    n_val = int(round(len(rows) * val_frac))
    val_idx = set(idx[:n_val])
    train_rows, train_labels, val_rows, val_labels = [], [], [], []
    for i in idx:
        if i in val_idx:
            val_rows.append(rows[i])
            val_labels.append(labels[i])
        else:
            train_rows.append(rows[i])
            train_labels.append(labels[i])
    return train_rows, train_labels, val_rows, val_labels
