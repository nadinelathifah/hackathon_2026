"""Step 1 modelling utilities: a self-contained NumPy logistic regression plus
the out-of-time evaluation and leave-one-out importance used to prove/prune the
feature set. Metrics come from the proven obcredit.modeling.metrics.
"""
from __future__ import annotations
from typing import List, Tuple

import numpy as np
import pandas as pd

from obcredit.modeling.metrics import roc_auc, gini_stability


class NumpyLogReg:
    """Plain L2-regularised logistic regression (full-batch gradient descent)."""

    def __init__(self, lr: float = 0.3, n_iter: int = 1500, l2: float = 1e-2):
        self.lr, self.n_iter, self.l2 = lr, n_iter, l2
        self.w = None
        self.b = 0.0

    def fit(self, X: np.ndarray, y: np.ndarray) -> "NumpyLogReg":
        n, d = X.shape
        self.w = np.zeros(d)
        self.b = 0.0
        for _ in range(self.n_iter):
            z = X @ self.w + self.b
            p = 1.0 / (1.0 + np.exp(-z))
            g = p - y
            self.w -= self.lr * ((X.T @ g) / n + self.l2 * self.w)
            self.b -= self.lr * float(g.mean())
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-(X @ self.w + self.b)))


def prepare(train: pd.DataFrame, test: pd.DataFrame,
            cols: List[str]) -> Tuple[np.ndarray, np.ndarray]:
    """Median-impute (train medians) then z-score (train mean/std). Returns the
    standardised train and test matrices."""
    # coerce to numeric first: a feature can legitimately be all-None for a
    # population (e.g. declared attributes when none were provided), which would
    # otherwise leave an object-dtype column and break the numeric model.
    tr_raw = train[cols].apply(pd.to_numeric, errors="coerce")
    te_raw = test[cols].apply(pd.to_numeric, errors="coerce")
    med = tr_raw.median().fillna(0.0)   # all-missing column -> impute 0
    tr = tr_raw.fillna(med)
    te = te_raw.fillna(med)
    mu = tr.mean()
    sd = tr.std(ddof=0).replace(0, 1.0)
    return (((tr - mu) / sd).to_numpy(dtype=float),
            ((te - mu) / sd).to_numpy(dtype=float))


def verdict(g: float) -> str:
    if g < 0.02:
        return "NO SIGNAL"
    if g < 0.10:
        return "WEAK"
    if g < 0.40:
        return "REAL SIGNAL"
    return "STRONG"


def fit_eval(train: pd.DataFrame, test: pd.DataFrame, cols: List[str],
             target: str = "target") -> dict:
    """Train on `train`, score `test`; return AUC/Gini train+test and the fitted
    model. Standardised coefficients let you read each feature's risk direction.
    """
    Xtr, Xte = prepare(train, test, cols)
    ytr = train[target].to_numpy(dtype=float)
    yte = test[target].to_numpy(dtype=float)
    model = NumpyLogReg().fit(Xtr, ytr)
    ptr, pte = model.predict_proba(Xtr), model.predict_proba(Xte)
    return {
        "model": model,
        "train_auc": roc_auc(ytr, ptr),
        "train_gini": 2 * roc_auc(ytr, ptr) - 1,
        "test_auc": roc_auc(yte, pte),
        "test_gini": 2 * roc_auc(yte, pte) - 1,
        "test_pred": pte,
        "coef": dict(zip(cols, model.w.tolist())),
    }


def leave_one_out(train: pd.DataFrame, test: pd.DataFrame, cols: List[str],
                  target: str = "target") -> List[Tuple[str, float]]:
    """Drop-column importance: Gini lost when each feature is removed. Positive =
    the feature adds unique signal; <= 0 = redundant / safe to prune."""
    full = fit_eval(train, test, cols, target)["test_gini"]
    out = []
    for c in cols:
        rest = [x for x in cols if x != c]
        if not rest:
            out.append((c, full))
            continue
        g = fit_eval(train, test, rest, target)["test_gini"]
        out.append((c, full - g))
    return sorted(out, key=lambda kv: kv[1], reverse=True)
