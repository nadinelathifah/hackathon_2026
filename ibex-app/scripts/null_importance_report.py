#!/usr/bin/env python3
r"""Null-importance report -- per-feature REAL gain vs its OWN null distribution.

GOES IN: scripts/null_importance_report.py

WHY THIS EXISTS
---------------
artifacts_v5/scorecard.json records the null-importance PARAMETERS (30 shuffles,
95th percentile) and the OUTCOME (which features were dropped), but the
per-feature table -- real training gain vs the 95th percentile of that feature's
own null distribution -- was computed inside the run and never persisted. This
script recomputes that table from the CACHED matrix (nothing is rebuilt from
Kaggle) and writes it to CSV. It faithfully mirrors gates 1-2 of
scripts/retrain_v2.py on the same candidate pool; the scorecard remains the
authoritative record of what the production funnel kept.

THE METHOD (the bit for the report)
-----------------------------------
Gate 1 (univariate screen): each feature alone must rank-order defaulters with
|Gini| >= 0.005 on the fit slice -- cheap removal of features with no
standalone signal.

Gate 2 (null importance): the target is shuffled --null-runs times and a
LightGBM model is retrained on each shuffle with identical hyperparameters and
a FIXED number of rounds. This yields, for every feature, the distribution of
training gain it achieves WHEN THERE IS NO SIGNAL AT ALL. A feature survives
only if its real gain exceeds the --null-pct percentile of ITS OWN null
distribution. Judging each feature against its own null (rather than one shared
threshold) is the Altmann et al. (2010) correction: raw gain is biased toward
high-cardinality features, and each feature's null distribution prices exactly
that bias. 30 shuffles is what makes a 95th percentile estimable at all.

USAGE (from the repo root)
--------------------------
    py -3.13 scripts\null_importance_report.py ^
        --ob "C:\Users\Josep\Downloads\obcache_b22\ob_matrix_full_all.pkl" ^
        --out artifacts_v5\null_importance_report.csv
    # wiring check (~2 min, numbers meaningless):
    py -3.13 scripts\null_importance_report.py --ob "..." --smoke

Runtime: (1 + null-runs) LightGBM fits on ~733k rows -- expect 15-30 minutes.
"""
from __future__ import annotations
import argparse
import os
import sys
import time

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

TARGET = "target"
WEEKCOL = "__week__"
POLICY_DROP = ("declared_education_code",)   # fairness proxy; never enters a gate

PARAMS = dict(objective="binary", learning_rate=0.05, num_leaves=31,
              max_depth=-1, min_child_samples=50, feature_fraction=0.8,
              bagging_fraction=0.8, bagging_freq=1, lambda_l1=0.0,
              lambda_l2=2.0, max_bin=255, num_threads=0, verbose=-1)


def split_rows(m: pd.DataFrame):
    """Chronological by origination week, then 60/20/20 by rows; fit/val is
    80/20 of train. Mirrors retrain_v2.three_way_split."""
    m = m.sort_values(WEEKCOL, kind="mergesort")
    n = len(m)
    tr = m.iloc[: int(0.6 * n)]
    k = int(0.8 * len(tr))
    return tr.iloc[:k], tr.iloc[k:]          # fit, val


def signed_gini(y: np.ndarray, s: np.ndarray) -> float:
    """Gini = 2*AUC - 1 (Mann-Whitney), NaN pairs dropped."""
    ok = ~np.isnan(s)
    y, s = y[ok], s[ok]
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0 or len(y) < 10:
        return float("nan")
    r = pd.Series(s).rank(method="average").to_numpy()
    auc = (r[y == 1].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
    return float(2.0 * auc - 1.0)


def _fit_gain(X, y, rounds, monotone, seed, spw, device):
    import lightgbm as lgb  # lazy: lets the rest of the script be tested alone
    p = dict(PARAMS)
    p["seed"] = seed
    p["scale_pos_weight"] = spw
    if device:
        p["device"] = device
    if monotone is not None:
        p["monotone_constraints"] = monotone
    ds = lgb.Dataset(X, label=y, feature_name=list(X.columns))
    return lgb.train(p, ds, num_boost_round=rounds).feature_importance(
        importance_type="gain")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ob", required=True, help="cached OB matrix pickle")
    ap.add_argument("--out", default="null_importance_report.csv")
    ap.add_argument("--null-runs", type=int, default=30)
    ap.add_argument("--null-rounds", type=int, default=200)
    ap.add_argument("--null-pct", type=float, default=95.0)
    ap.add_argument("--min-iv", type=float, default=0.005)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--no-monotone", action="store_true")
    ap.add_argument("--smoke", action="store_true",
                    help="2 runs / 50 rounds / 40k rows: wiring check only")
    args = ap.parse_args()

    if not os.path.exists(args.ob):
        raise SystemExit(f"FATAL: --ob file not found: {args.ob}")

    from obcredit.feature_registry import REGISTRY  # noqa
    import obcredit.feature_functions  # noqa: F401  (registration side-effect)
    from obcredit.missingness import add_missingness, MISSINGNESS_MONOTONE

    print("[null] loading OB matrix:", args.ob)
    m = pd.read_pickle(args.ob)
    print(f"[null] matrix {m.shape[0]:,} rows x {m.shape[1]} cols")

    base_cols = [c for c in REGISTRY.parity_names() if c in m.columns]
    m = add_missingness(m, base_cols, copy=False)
    mono_map = REGISTRY.monotone_map()
    mono_map.update(MISSINGNESS_MONOTONE)
    cols = [c for c in base_cols + list(MISSINGNESS_MONOTONE)
            if c in m.columns and c not in POLICY_DROP]
    print(f"[null] candidate pool: {len(cols)} (parity + 4 flags, "
          f"policy-dropped: {', '.join(POLICY_DROP)})")

    fit, val = split_rows(m)
    y_fit = fit[TARGET].astype(int).to_numpy()
    spw = float((y_fit == 0).sum() / max(1, (y_fit == 1).sum()))
    print(f"[null] fit={len(fit):,} val={len(val):,} "
          f"fit base rate={y_fit.mean():.5f} scale_pos_weight={spw:.2f}")

    if args.smoke:
        fit = fit.sample(n=min(40_000, len(fit)), random_state=args.seed)
        y_fit = fit[TARGET].astype(int).to_numpy()
        args.null_runs, args.null_rounds = 2, 50
        print("[null] SMOKE MODE: 40k rows, 2 runs, 50 rounds -- numbers are junk")

    X = fit[cols]
    monotone = None if args.no_monotone else [int(mono_map.get(c, 0)) for c in cols]

    print("[null] gate 1: univariate screen (|Gini| on fit) ...")
    uni = {c: signed_gini(y_fit, pd.to_numeric(fit[c], errors="coerce").to_numpy(float))
           for c in cols}
    gate1 = [c for c in cols if abs(uni.get(c) or 0.0) >= args.min_iv]
    print(f"[null] gate 1: -{len(cols) - len(gate1)} -> {len(gate1)} kept")

    t0 = time.time()
    print(f"[null] gate 2: 1 real fit + {args.null_runs} null fits "
          f"({args.null_rounds} fixed rounds each) ...")
    Xg = X[gate1]
    real_gain = np.asarray(_fit_gain(Xg, y_fit, args.null_rounds, monotone,
                                     args.seed, spw, args.device), dtype=float)
    nulls = np.zeros((args.null_runs, len(gate1)))
    rng = np.random.default_rng(args.seed)
    for r in range(args.null_runs):
        y_shuf = rng.permutation(y_fit)
        nulls[r] = _fit_gain(Xg, y_shuf, args.null_rounds, monotone,
                             args.seed + 1000 + r, spw, args.device)
        rate = (r + 2) / max(1e-9, time.time() - t0)
        eta = (args.null_runs - r - 1) / rate if rate else 0
        print(f"[null]   shuffle {r + 1}/{args.null_runs} "
              f"(eta {eta / 60:0.1f} min)", flush=True)

    null_mean = nulls.mean(axis=0)
    null_p = np.percentile(nulls, args.null_pct, axis=0)

    rep = pd.DataFrame({
        "feature": gate1,
        "uni_gini_fit": [uni[c] for c in gate1],
        "gate1_survived": True,
        "real_gain": real_gain,
        "null_gain_mean": null_mean,
        "null_gain_p95": null_p,
        "verdict": np.where(real_gain > null_p, "KEEP", "DROP"),
    })
    dropped_g1 = pd.DataFrame({
        "feature": [c for c in cols if c not in gate1],
        "uni_gini_fit": [uni[c] for c in cols if c not in gate1],
        "gate1_survived": False,
        "real_gain": np.nan, "null_gain_mean": np.nan, "null_gain_p95": np.nan,
        "verdict": "DROP (gate 1: |Gini| below screen)",
    })
    rep = pd.concat([rep, dropped_g1], ignore_index=True)
    rep = rep.sort_values(["gate1_survived", "real_gain"],
                          ascending=[False, False]).reset_index(drop=True)

    kept = int((rep["verdict"] == "KEEP").sum())
    print(f"\n[null] gate 2: keep {kept} / {len(gate1)} "
          f"(real gain > {args.null_pct:0.1f}th pct of own null)")
    print(rep[rep["gate1_survived"]].to_string(index=False))
    rep.to_csv(args.out, index=False)
    print(f"\n[null] wrote {args.out}")
    if not args.smoke:
        print("[null] NOTE: production gates 3-4 (correlation clusters, then "
              "permutation on VAL) reduce this further -- see funnel_summary.csv "
              "and selection_report.csv for the final 16.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
