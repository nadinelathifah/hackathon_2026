#!/usr/bin/env python3
"""STEP 3b -- FIRST XGBoost model on the reconstructed feature matrix.

Trains on the OPEN-BANKING reconstruction (the representation inference will
actually see) so there is no train/serve skew, then reports:
  * out-of-time (split-by-week) train/test Gini
  * a comparison model trained on the Kaggle-direct matrix (feature-power ceiling)
  * permutation importance on the held-out test set (for pruning)

Requires xgboost:   pip install xgboost
(Everything else -- numpy, pandas, pyarrow -- you already have.)

Examples:
  python scripts/run_xgb.py "C:\\Users\\Josep\\Downloads\\homecredit"
  python scripts/run_xgb.py "...\\homecredit" --max-cases 25000 --perm-repeats 15
  python scripts/run_xgb.py "...\\homecredit" --source kaggle          # ceiling
  python scripts/run_xgb.py "...\\homecredit" --no-monotone            # ablation
  python scripts/run_xgb.py "...\\homecredit" --exclude total_overdue_amount,max_overdue_amount_24m,avg_overdue_amount_24m
"""
from __future__ import annotations
import argparse
import os
import sys

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from obcredit.adapters import KaggleAdapter
from obcredit.feature_registry import REGISTRY
from step3lib.kaggle_stream import canonical_pop_to_ground_truth
from step3lib.model_xgb import fit_eval_xgb, permutation_importance
# Reuse the exact loaders/reconstruction the fidelity runner uses (it has a
# __main__ guard, so importing does not trigger a run).
from run_step3 import read_labels, reconstruct

TARGET = "target"


def _split_by_week(matrix: pd.DataFrame, labels: dict, cols):
    """Attach target+week from labels, drop unlabelled rows, sort by week and cut
    80/20 -> an out-of-time split identical in spirit to run_step3._split_and_eval.
    labels maps str(case_id) -> (target, week).
    """
    data = matrix.copy()
    idx = [str(i) for i in matrix.index]
    data[TARGET] = [labels.get(i, (None, None))[0] for i in idx]
    data["__week__"] = [labels.get(i, (None, None))[1] for i in idx]
    data = data.dropna(subset=[TARGET, "__week__"]).sort_values("__week__")
    if data.empty:
        raise SystemExit("ERROR: no labelled rows after join -- check the label source.")
    cut = max(1, int(len(data) * 0.8))
    train, test = data.iloc[:cut], data.iloc[cut:]
    keep = [c for c in cols if c in data.columns]
    return train, test, keep


def _build_stamp() -> str:
    try:
        with open(os.path.join(_ROOT, "VERSION.txt"), "r", encoding="utf-8") as vf:
            return vf.readline().strip()
    except OSError:
        return "BUILD ??? (VERSION.txt not found)"


def main() -> int:
    ap = argparse.ArgumentParser(description="First XGBoost model on reconstructed features.")
    ap.add_argument("kaggle_dir")
    ap.add_argument("--max-cases", type=int, default=25000, help="0 = full population")
    ap.add_argument("--batch", type=int, default=4000)
    ap.add_argument("--source", choices=["ob", "kaggle"], default="ob",
                    help="ob = open-banking reconstruction (default, deployable); "
                         "kaggle = Kaggle-direct feature-power ceiling")
    ap.add_argument("--no-monotone", action="store_true",
                    help="disable monotone constraints (ablation)")
    ap.add_argument("--exclude", default="",
                    help="comma-separated features to drop (e.g. the low-fidelity "
                         "overdue-amount features)")
    ap.add_argument("--perm-repeats", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    try:
        import xgboost as xgb  # noqa: F401
    except ImportError:
        print("ERROR: xgboost is not installed.\n       Install it with:  pip install xgboost",
              file=sys.stderr)
        return 3

    if not os.path.isdir(args.kaggle_dir):
        print(f"ERROR: not a directory: {args.kaggle_dir}", file=sys.stderr)
        return 2

    max_cases = None if args.max_cases in (0, None) else args.max_cases

    print("=" * 78)
    print("STEP 3b: FIRST XGBoost on reconstructed features")
    print(f">>> RUNNING: {_build_stamp()}")
    print(f">>> xgboost {xgb.__version__} | source={args.source} | "
          f"monotone={'off' if args.no_monotone else 'on'}")
    print("=" * 78)

    print("\n[1/4] streaming real Kaggle data -> canonical applicants ...")
    labels = read_labels(args.kaggle_dir, max_cases=max_cases)
    adapter = KaggleAdapter.from_parquet_dir(args.kaggle_dir, max_cases=max_cases)
    applicants = list(adapter.stream_canonical())
    print(f"      {len(applicants):,} applicants")

    print("[2/4] converting to open-banking ground truth + reconstructing via shared f() ...")
    pop = canonical_pop_to_ground_truth(applicants, labels=labels)
    ob, kg = reconstruct(pop, batch=args.batch)
    matrix = ob if args.source == "ob" else kg
    print(f"      using {args.source} matrix {matrix.shape}")

    excluded = {c.strip() for c in args.exclude.split(",") if c.strip()}
    feats = [c for c in REGISTRY.parity_names()
             if c in ob.columns and c in kg.columns and c not in excluded]
    if excluded:
        print(f"      excluding {sorted(excluded & set(REGISTRY.parity_names()))}")

    print("[3/4] training XGBoost (out-of-time split by week) ...")
    train, test, cols = _split_by_week(matrix, labels, feats)
    monotone = None if args.no_monotone else REGISTRY.monotone_map()
    res = fit_eval_xgb(train, test, cols, target=TARGET, monotone=monotone, seed=args.seed)
    print(f"      train rows={len(train):,}  test rows={len(test):,}  features={len(cols)}")
    print(f"      best_iteration = {res['best_iteration']}")
    print(f"      XGBoost train Gini = {res['train_gini']:0.4f}")
    print(f"      XGBoost  test Gini = {res['test_gini']:0.4f}")

    print("\n[4/4] permutation importance on held-out test "
          f"({args.perm_repeats} repeats; positive = adds out-of-sample signal):")
    baseline, imp = permutation_importance(res, test, target=TARGET,
                                           n_repeats=args.perm_repeats, seed=args.seed)
    print(f"      baseline test Gini = {baseline:0.4f}")
    print(f"      {'feature':32s} {'perm_drop':>10s} {'std':>8s}")
    for name, drop, std in imp:
        print(f"      {name:32s} {drop:+10.4f} {std:8.4f}")

    prune = [n for n, d, _ in imp if d <= 0.0]
    if prune:
        print("\n      candidates to prune (perm importance <= 0):")
        print("        " + ", ".join(prune))

    print("\nDONE. First XGBoost model trained + ranked on REAL per-payment data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
