#!/usr/bin/env python3
"""Quick, dependency-free PROOF that the reconstructed features carry signal.

Trains a pure-NumPy logistic regression (no sklearn / xgboost needed) on the
feature matrix produced by run_kaggle.py, using an OUT-OF-TIME split, and
reports AUC / Gini and the Home Credit gini_stability metric. By DEFAULT it
trains ONLY on parity-safe features (those reconstructable from open banking),
so a good score is an honest estimate of achievable live performance -- directly
answering "will these features survive on TrueLayer data?".

Usage:
    python scripts/run_quick_proof.py <features.parquet|csv> <base_dir> [--all]

  <base_dir>  folder containing train_base.parquet (for target + WEEK_NUM).
  --all       also include non-parity-safe features (diagnostic only).

Criteria (test-set Gini): <0.02 NO SIGNAL | <0.10 WEAK | <0.40 REAL | else STRONG.
"""
from __future__ import annotations
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obcredit import feature_functions as _ff  # noqa: F401,E402 (registers features)
from obcredit.feature_registry import REGISTRY  # noqa: E402
from obcredit.modeling.dataset import CreditDataset  # noqa: E402
from obcredit.modeling.metrics import roc_auc, gini_stability  # noqa: E402


class NumpyLogReg:
    """Plain-vanilla L2-regularised logistic regression via gradient descent."""

    def __init__(self, lr: float = 0.3, n_iter: int = 1500, l2: float = 1e-2):
        self.lr, self.n_iter, self.l2 = lr, n_iter, l2
        self.w = None
        self.b = 0.0

    def fit(self, X, y):
        n, d = X.shape
        self.w = np.zeros(d)
        self.b = 0.0
        for _ in range(self.n_iter):
            z = X @ self.w + self.b
            p = 1.0 / (1.0 + np.exp(-z))
            g = p - y
            gw = X.T @ g / n + self.l2 * self.w
            gb = g.mean()
            self.w -= self.lr * gw
            self.b -= self.lr * gb
        return self

    def predict_proba(self, X):
        return 1.0 / (1.0 + np.exp(-(X @ self.w + self.b)))


def _prepare(Xtr, Xva):
    """Median-impute (fit on train) then z-score (fit on train)."""
    med = np.nanmedian(Xtr, axis=0)
    med = np.where(np.isnan(med), 0.0, med)
    Xtr = np.where(np.isnan(Xtr), med, Xtr)
    Xva = np.where(np.isnan(Xva), med, Xva)
    mu = Xtr.mean(axis=0)
    sd = Xtr.std(axis=0)
    sd = np.where(sd < 1e-9, 1.0, sd)
    return (Xtr - mu) / sd, (Xva - mu) / sd


def _verdict(gini: float) -> str:
    if gini < 0.02:
        return "NO SIGNAL"
    if gini < 0.10:
        return "WEAK"
    if gini < 0.40:
        return "REAL SIGNAL"
    return "STRONG"


def _fit_eval(X, y, week, tr, va, cols_idx):
    """Train on the given column subset and return the out-of-time test Gini."""
    Xtr, Xva = _prepare(X[tr][:, cols_idx], X[va][:, cols_idx])
    model = NumpyLogReg().fit(Xtr, y[tr])
    return 2 * roc_auc(y[va], model.predict_proba(Xva)) - 1


def main():
    argv = sys.argv[1:]
    use_all = "--all" in argv
    use_loo = "--leave-one-out" in argv or "--loo" in argv
    args = [a for a in argv if not a.startswith("--")]
    if len(args) < 2:
        print(__doc__)
        sys.exit(1)
    features_path, base_dir = args[0], args[1]

    ds = CreditDataset.from_files(features_path, base_dir)
    parity_safe = set(REGISTRY.parity_names())
    cols = [c for c in ds.feature_names if use_all or c in parity_safe]
    if not cols:
        print("no usable feature columns found")
        sys.exit(1)

    X = ds.features[cols].to_numpy(dtype=float)
    y = ds.target.to_numpy(dtype=float)
    week = ds.week.to_numpy(dtype=float)

    cut = np.quantile(week, 0.8)
    tr = week <= cut
    va = week > cut
    if tr.sum() == 0 or va.sum() == 0:   # degenerate week column -> random split
        rng = np.random.default_rng(0)
        idx = rng.permutation(len(y))
        va = np.zeros(len(y), bool)
        va[idx[: max(1, len(y) // 5)]] = True
        tr = ~va

    print(f"rows={len(y):,}  default={y.mean():.3%}  "
          f"train={int(tr.sum()):,}  test={int(va.sum()):,}")
    print(f"features ({'ALL' if use_all else 'parity-safe only'}): {len(cols)}")

    Xtr, Xva = _prepare(X[tr], X[va])
    model = NumpyLogReg().fit(Xtr, y[tr])
    p_tr = model.predict_proba(Xtr)
    p_va = model.predict_proba(Xva)
    auc_tr = roc_auc(y[tr], p_tr)
    auc_va = roc_auc(y[va], p_va)
    g_va = 2 * auc_va - 1
    stab = gini_stability(week[va], y[va], p_va)

    print(f"\nTRAIN  AUC {auc_tr:.4f}  Gini {2 * auc_tr - 1:.4f}")
    print(f"TEST   AUC {auc_va:.4f}  Gini {g_va:.4f}")
    print(f"gini_stability {stab['metric']:.4f}  "
          f"(mean {stab['mean_gini']:.4f}, slope {stab['slope']:.5f})")
    print(f"VERDICT: {_verdict(g_va)}")

    print("\nunivariate |Gini| per feature (* = parity-safe):")
    rows = []
    for j, c in enumerate(cols):
        xj = X[:, j].astype(float)
        m = ~np.isnan(xj)
        if m.sum() < 10 or len(np.unique(y[m])) < 2:
            continue
        rows.append((abs(2 * roc_auc(y[m], xj[m]) - 1), c))
    for g, c in sorted(rows, reverse=True):
        mark = "*" if c in parity_safe else " "
        print(f"  {mark} {c:30s} {g:.4f}")

    if use_loo:
        # Leave-one-out (drop-column) importance: how much test Gini FALLS when
        # each feature is removed. Positive drop => the feature adds unique
        # signal; ~0 or negative => redundant or noise (safe to drop).
        print("\nleave-one-out importance (Gini drop when feature removed):")
        full_idx = list(range(len(cols)))
        base_gini = _fit_eval(X, y, week, tr, va, full_idx)
        print(f"  full model test Gini = {base_gini:.4f}")
        drops = []
        for j, c in enumerate(cols):
            subset = [k for k in full_idx if k != j]
            gj = _fit_eval(X, y, week, tr, va, subset)
            drops.append((base_gini - gj, c))
        for d, c in sorted(drops, reverse=True):
            mark = "*" if c in parity_safe else " "
            flag = "  <- drop candidate" if d <= 0 else ""
            print(f"  {mark} {c:30s} {d:+.4f}{flag}")


if __name__ == "__main__":
    main()
