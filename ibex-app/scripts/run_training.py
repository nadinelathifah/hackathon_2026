#!/usr/bin/env python3
"""Train the real model (XGBoost) on the parity-safe features, out-of-time.

Usage:
    python scripts/run_training.py <features.parquet|csv> <base_dir> [--all]

Requires: pip install xgboost
By default trains ONLY on parity-safe (open-banking-reconstructable) features and
applies the registry's monotone constraints. This is the step AFTER run_quick_proof
confirms the features carry signal.
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


def main():
    argv = sys.argv[1:]
    use_all = "--all" in argv
    args = [a for a in argv if not a.startswith("--")]
    if len(args) < 2:
        print(__doc__)
        sys.exit(1)
    features_path, base_dir = args[0], args[1]

    ds = CreditDataset.from_files(features_path, base_dir)
    parity_safe = set(REGISTRY.parity_names())
    cols = [c for c in ds.feature_names if use_all or c in parity_safe]
    mono = REGISTRY.monotone_map()
    constraints = tuple(mono.get(c, 0) for c in cols)

    X = ds.features[cols].to_numpy(dtype=float)
    y = ds.target.to_numpy(dtype=float)
    week = ds.week.to_numpy(dtype=float)
    cut = np.quantile(week, 0.8)
    tr, va = week <= cut, week > cut
    print(f"rows={len(y):,}  default={y.mean():.3%}  "
          f"train={int(tr.sum()):,}  valid={int(va.sum()):,}  "
          f"features ({'ALL' if use_all else 'parity-safe'})={len(cols)}")

    try:
        import xgboost as xgb
    except ImportError:
        print("\nxgboost not installed. Run: pip install xgboost")
        sys.exit(1)

    dtr = xgb.DMatrix(X[tr], label=y[tr], feature_names=cols)
    dva = xgb.DMatrix(X[va], label=y[va], feature_names=cols)
    spw = float((y[tr] == 0).sum()) / max(1.0, float((y[tr] == 1).sum()))
    params = {
        "objective": "binary:logistic", "eval_metric": "auc",
        "eta": 0.02, "max_depth": 4, "min_child_weight": 20,
        "subsample": 0.8, "colsample_bytree": 0.8,
        "alpha": 1.0, "lambda": 5.0, "tree_method": "hist",
        "scale_pos_weight": spw, "monotone_constraints": str(constraints),
    }
    model = xgb.train(params, dtr, num_boost_round=2000,
                      evals=[(dtr, "train"), (dva, "valid")],
                      early_stopping_rounds=100, verbose_eval=100)
    p_va = model.predict(dva)
    auc = roc_auc(y[va], p_va)
    stab = gini_stability(week[va], y[va], p_va)
    print(f"\nVALID  AUC {auc:.4f}  Gini {2 * auc - 1:.4f}  "
          f"gini_stability {stab['metric']:.4f}")
    print("\ntop feature gains:")
    for f, v in sorted(model.get_score(importance_type="gain").items(),
                       key=lambda kv: -kv[1])[:20]:
        print(f"  {f:32s} {v:.1f}")


if __name__ == "__main__":
    main()
