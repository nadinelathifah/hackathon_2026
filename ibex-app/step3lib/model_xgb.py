"""XGBoost model for Step 3 -- trained on the SAME reconstructed feature matrix
that inference would see (open banking), so there is no train/serve skew.

Design choices (defensible):
  * Native ``xgboost.train`` API + ``DMatrix`` -- needs ONLY the xgboost wheel,
    NOT scikit-learn (which isn't a project dependency).
  * Monotone constraints pulled straight from REGISTRY.monotone_map() so risk
    direction is enforced (more arrears -> higher risk, protective features ->
    lower). This is what a model-risk reviewer expects for a credit scorecard.
  * scale_pos_weight = neg/pos to handle the low default rate.
  * Median-impute using TRAIN medians only (no leakage), same policy as the
    logistic-regression baseline in model.py.
  * Permutation importance is implemented here in pure NumPy (shuffle one column,
    measure the Gini drop on the held-out test set, averaged over repeats). This
    is model-agnostic, needs no sklearn, and is the honest way to rank features
    for pruning -- unlike XGBoost's internal 'gain', it reflects out-of-sample
    predictive contribution.

All Gini/AUC come from the proven obcredit.modeling.metrics (pure NumPy).
"""
from __future__ import annotations
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from obcredit.modeling.metrics import roc_auc


def _gini(y, p) -> float:
    return 2.0 * roc_auc(y, p) - 1.0


def _prep(train: pd.DataFrame, test: pd.DataFrame, cols: List[str]):
    """Median-impute with TRAIN medians only. Returns (train_np, test_np, medians)."""
    med = train[cols].median(numeric_only=True)
    tr = train[cols].fillna(med)
    te = test[cols].fillna(med)
    # any column that was entirely NaN in train -> median is NaN -> fill 0
    tr = tr.fillna(0.0)
    te = te.fillna(0.0)
    return tr.to_numpy(dtype=float), te.to_numpy(dtype=float), med


def default_params(seed: int = 42) -> dict:
    """Conservative, shallow, well-regularised params suited to a small,
    imbalanced credit dataset (guards against overfitting the 25k harness)."""
    return {
        "objective": "binary:logistic",
        "eval_metric": "auc",
        "max_depth": 3,
        "eta": 0.05,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "min_child_weight": 5.0,
        "lambda": 2.0,
        "alpha": 0.0,
        "tree_method": "hist",
        "seed": seed,
    }


def fit_eval_xgb(train: pd.DataFrame, test: pd.DataFrame, cols: List[str],
                 target: str = "target",
                 monotone: Optional[Dict[str, int]] = None,
                 params: Optional[dict] = None,
                 num_boost_round: int = 600,
                 early_stopping_rounds: int = 40,
                 seed: int = 42) -> dict:
    """Train XGBoost on `train`, early-stop + score on `test`.

    monotone: optional {feature_name: -1|0|+1}. Constraints are emitted in the
    exact order of `cols` (the DMatrix column order), which is required for the
    tuple form to line up with the right features.
    Returns a dict with the booster, train/test Gini, predictions, best_iteration,
    the train medians (needed to score new data identically), and gain importance.
    """
    import xgboost as xgb

    Xtr, Xte, med = _prep(train, test, cols)
    ytr = train[target].to_numpy(dtype=float)
    yte = test[target].to_numpy(dtype=float)

    dtr = xgb.DMatrix(Xtr, label=ytr, feature_names=list(cols))
    dte = xgb.DMatrix(Xte, label=yte, feature_names=list(cols))

    p = default_params(seed=seed)
    if params:
        p.update(params)
    pos = float((ytr == 1).sum())
    neg = float((ytr == 0).sum())
    p["scale_pos_weight"] = (neg / pos) if pos > 0 else 1.0
    if monotone:
        p["monotone_constraints"] = "(" + ",".join(str(int(monotone.get(c, 0))) for c in cols) + ")"

    booster = xgb.train(
        p, dtr, num_boost_round=num_boost_round,
        evals=[(dtr, "train"), (dte, "test")],
        early_stopping_rounds=early_stopping_rounds,
        verbose_eval=False,
    )
    best_it = getattr(booster, "best_iteration", None)
    rng = {"iteration_range": (0, int(best_it) + 1)} if best_it is not None else {}
    ptr = booster.predict(dtr, **rng)
    pte = booster.predict(dte, **rng)

    return {
        "model": booster,
        "medians": med,
        "cols": list(cols),
        "params": p,
        "best_iteration": (int(best_it) if best_it is not None else None),
        "train_gini": _gini(ytr, ptr),
        "test_gini": _gini(yte, pte),
        "test_pred": pte,
        "gain_importance": booster.get_score(importance_type="gain"),
    }


def permutation_importance(result: dict, test: pd.DataFrame,
                           target: str = "target",
                           n_repeats: int = 10,
                           seed: int = 42) -> Tuple[float, List[Tuple[str, float, float]]]:
    """Model-agnostic permutation importance on the HELD-OUT test set.

    For each feature: shuffle its column `n_repeats` times, measure how much the
    test Gini drops. Positive mean drop => the feature carries real out-of-sample
    signal; ~0 or negative => redundant / safe to prune.
    Returns (baseline_test_gini, [(feature, mean_drop, std_drop) sorted desc]).
    """
    import xgboost as xgb

    cols = result["cols"]
    med = result["medians"]
    booster = result["model"]
    best_it = result.get("best_iteration")
    rng = {"iteration_range": (0, int(best_it) + 1)} if best_it is not None else {}

    X = test[cols].fillna(med).fillna(0.0).to_numpy(dtype=float)
    y = test[target].to_numpy(dtype=float)

    def gini_of(mat: np.ndarray) -> float:
        d = xgb.DMatrix(mat, feature_names=list(cols))
        return _gini(y, booster.predict(d, **rng))

    baseline = gini_of(X)
    rs = np.random.RandomState(seed)
    out: List[Tuple[str, float, float]] = []
    for j, c in enumerate(cols):
        drops = []
        for _ in range(n_repeats):
            m = X.copy()
            m[:, j] = rs.permutation(m[:, j])
            drops.append(baseline - gini_of(m))
        out.append((c, float(np.mean(drops)), float(np.std(drops))))
    out.sort(key=lambda kv: kv[1], reverse=True)
    return baseline, out
