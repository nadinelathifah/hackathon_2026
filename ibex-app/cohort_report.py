#!/usr/bin/env python3
"""Score a random cohort from the UNTOUCHED eval slice and characterise them.

What this answers
-----------------
"Does a different person get a different score, and does the score move in the
direction a human would expect?" Until now you have only ever seen ONE live
number (577) from ONE sandbox identity, which tells you nothing about spread.

This samples N>=100 real applicants at random from the eval slice -- the last
20% by competition week, which the model never saw during training OR
calibration -- scores them through the exact production path, and labels each
one in plain English (thin file, thick clean, arrears, etc.) so you can check
that the ordering is sane.

ob -> ob discipline
-------------------
The matrix this reads is the OB-RECONSTRUCTED one: Kaggle ground truth rendered
into TrueLayer payloads, put through TrueLayerAdapter, then through the same
FeaturePipeline the live endpoint uses. So these feature vectors are produced
the same way a real TrueLayer connection produces them. Do NOT point this at
the kg matrix -- that would be measuring a model you do not deploy.

Scoring path is identical to serving:
    X = matrix[features].fillna(train_medians).fillna(0.0)
    raw = booster.predict(X, num_iteration=best_iteration)
    pd  = calibrator.predict(raw)
    score = pd_to_score(pd)

Usage
-----
  py -3.13 scripts/cohort_report.py --ob "C:\\...\\ob_matrix_full_all.pkl" -n 150
  py -3.13 scripts/cohort_report.py --ob "..." -n 200 --csv artifacts/cohort.csv
"""
from __future__ import annotations
import argparse
import json
import os
import pickle
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_LGBM2 = os.path.join(_ROOT, "lgbm_2")
for _p in (_ROOT, _LGBM2, os.path.join(_ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from obcredit.modeling.calibration import IsotonicCalibrator  # noqa: E402
from obcredit.modeling.scorecard import pd_to_score, score_to_band  # noqa: E402

TARGET = "target"
WEEKCOL = "__week__"


def three_way_split(m, f_train=0.6, f_calib=0.2):
    """Byte-identical to calibrate_score.three_way_split -- chronological."""
    sub = m.dropna(subset=[TARGET, WEEKCOL])
    order = list(sub.sort_values(WEEKCOL).index)
    n = len(order)
    return (order[:int(n * f_train)],
            order[int(n * f_train):int(n * (f_train + f_calib))],
            order[int(n * (f_train + f_calib)):])


def load_artifacts(art_dir):
    import lightgbm as lgb
    sc_path = os.path.join(art_dir, "scorecard.json")
    if not os.path.exists(sc_path):
        raise SystemExit(f"no scorecard.json in {art_dir} -- run calibrate_score.py first.")
    with open(sc_path, "r", encoding="utf-8") as f:
        sc = json.load(f)
    booster = lgb.Booster(model_file=os.path.join(art_dir, "model_lgbm.txt"))
    cal = IsotonicCalibrator.load(os.path.join(art_dir, "calibrator.pkl"))
    return sc, booster, cal


def _num(row, name, default=np.nan):
    v = row.get(name, default)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return np.nan
    return v if np.isfinite(v) else np.nan


def characterise(row, feature_cols):
    """Plain-English description of ONE applicant, from OB features only.

    Deliberately uses only things a lender could actually see in an open
    banking feed -- obligations, arrears history, detected income, balances.
    No labels, no model output. This is the independent description you check
    the score against.
    """
    obl = _num(row, "num_active_obligations")
    dpd = _num(row, "max_dpd_24m")
    ser = _num(row, "num_serious_arrears_24m")
    ndpd = _num(row, "num_dpd_events_24m")
    inc = _num(row, "monthly_income")
    dti = _num(row, "debt_to_income")
    pti = _num(row, "payment_to_income")
    minb = _num(row, "min_balance_3m")
    streak = _num(row, "longest_clean_streak_24m")
    cv = _num(row, "cv_payment_amount")

    present = int(sum(1 for c in feature_cols if pd.notna(row.get(c))))
    coverage = present / max(1, len(feature_cols))

    # ---- file thickness -------------------------------------------------- #
    if not np.isfinite(obl) or obl <= 0:
        thickness = "no-file"
    elif obl <= 2:
        thickness = "thin"
    elif obl <= 5:
        thickness = "medium"
    else:
        thickness = "thick"
    if coverage < 0.5 and thickness in ("no-file", "thin"):
        thickness = "ultra-thin"

    # ---- payment conduct -------------------------------------------------- #
    if np.isfinite(ser) and ser > 0:
        conduct = "serious arrears"
    elif np.isfinite(dpd) and dpd >= 30:
        conduct = "30d+ late"
    elif np.isfinite(ndpd) and ndpd > 0:
        conduct = "minor late"
    elif np.isfinite(streak) and streak >= 18:
        conduct = "long clean"
    elif thickness in ("no-file", "ultra-thin"):
        conduct = "no history"
    else:
        conduct = "clean"

    # ---- affordability ---------------------------------------------------- #
    if not np.isfinite(inc) or inc <= 0:
        afford = "income undetected"
    elif np.isfinite(pti) and pti > 0.45:
        afford = "stretched"
    elif np.isfinite(dti) and dti > 6.0:
        afford = "high debt"
    elif np.isfinite(minb) and minb < 0:
        afford = "overdrawn"
    else:
        afford = "comfortable"

    volatile = bool(np.isfinite(cv) and cv > 0.5)
    parts = [thickness, conduct, afford] + (["volatile income"] if volatile else [])
    return {"thickness": thickness, "conduct": conduct, "afford": afford,
            "coverage": coverage, "obligations": obl, "income": inc,
            "max_dpd": dpd, "dti": dti, "label": " / ".join(parts)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Score + characterise a random eval cohort.")
    ap.add_argument("--ob", required=True,
                    help="OB-reconstructed matrix pickle (ob_matrix_full_all.pkl)")
    ap.add_argument("--artifacts", default=os.path.join(_ROOT, "artifacts"))
    ap.add_argument("-n", "--n", type=int, default=150)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--csv", default=None)
    ap.add_argument("--show", type=int, default=40,
                    help="how many individual applicants to print")
    args = ap.parse_args()

    sc, booster, cal = load_artifacts(args.artifacts)
    feats = list(sc["features"])
    meds = sc.get("medians", {})
    best = int(sc.get("best_iteration", 0)) or None
    pdo = float(sc.get("pdo", 40.0))
    base_score = float(sc.get("base_score", 600.0))
    base_odds = float(sc.get("base_odds", 20.0))
    print(f"artifacts : build {sc.get('build')}  frame {sc.get('frame')}  "
          f"{len(feats)} features  best_iter {best}")
    if sc.get("frame") != "ob":
        print("  WARNING: artifacts were trained on the kg frame, not ob.")

    with open(args.ob, "rb") as f:
        m = pickle.load(f)
    m.index = [str(i) for i in m.index]
    print(f"matrix    : {len(m):,} rows")

    missing = [c for c in feats if c not in m.columns]
    if missing:
        raise SystemExit(f"matrix is missing {len(missing)} model features: {missing[:6]}")

    _, _, ev_ids = three_way_split(m)
    print(f"eval slice: {len(ev_ids):,} rows (last 20% by week, never trained on)")
    if len(ev_ids) < args.n:
        raise SystemExit(f"eval slice only has {len(ev_ids)} rows")

    rng = np.random.default_rng(args.seed)
    pick = list(rng.choice(np.asarray(ev_ids, dtype=object), args.n, replace=False))
    coh = m.loc[pick]

    X = coh[feats].apply(pd.to_numeric, errors="coerce")
    for c in feats:
        X[c] = X[c].fillna(meds.get(c, 0.0))
    X = X.fillna(0.0)

    raw = booster.predict(X.to_numpy(np.float32), num_iteration=best)
    pdv = np.asarray(cal.predict(raw), dtype=float)
    scores = np.asarray(pd_to_score(pdv, pdo, base_score, base_odds), dtype=float)
    bands = score_to_band(scores)

    rows = []
    for i, cid in enumerate(pick):
        d = characterise(coh.loc[cid], feats)
        d.update({"case_id": str(cid), "pd": float(pdv[i]),
                  "score": float(scores[i]), "band": bands[i],
                  "actual_default": coh.loc[cid].get(TARGET)})
        rows.append(d)
    out = pd.DataFrame(rows).sort_values("score", ascending=False)

    print("\n=================== INDIVIDUAL APPLICANTS ===================")
    print(f"{'case_id':>10s} {'score':>6s} {'bd':>2s} {'PD':>7s}  {'obl':>4s} "
          f"{'income':>8s}  description")
    print("-" * 96)
    half = max(1, args.show // 2)
    show = pd.concat([out.head(half), out.tail(half)])
    prev_i = None
    for i, (_, r) in enumerate(show.iterrows()):
        if prev_i == half - 1:
            print(f"{'':>10s} {'...':>6s}   ({len(out) - args.show:,} more)")
        ob_s = "-" if not np.isfinite(r["obligations"]) else f"{r['obligations']:0.0f}"
        in_s = "-" if not np.isfinite(r["income"]) else f"{r['income']:8.0f}"
        print(f"{r['case_id'][:10]:>10s} {r['score']:6.1f} {r['band']:>2s} "
              f"{r['pd']:7.4f}  {ob_s:>4s} {in_s:>8s}  {r['label']}")
        prev_i = i

    print("\n=================== SCORE DISTRIBUTION ===================")
    print(f"  n              : {len(out)}")
    print(f"  min / max      : {out['score'].min():0.1f} / {out['score'].max():0.1f}")
    print(f"  spread         : {out['score'].max() - out['score'].min():0.1f} points")
    for q in (5, 25, 50, 75, 95):
        print(f"  p{q:<2d}            : {np.percentile(out['score'], q):0.1f}")
    print("\n  bands:")
    for b in ("A", "B", "C", "D", "E"):
        k = int((out["band"] == b).sum())
        bar = "#" * int(40 * k / max(1, len(out)))
        print(f"    {b}: {k:4d}  {bar}")
    if int((out["band"] == "A").sum()) == 0:
        print("    (no band A -- expected while the PD floor sits above 0.0062)")

    print("\n=================== BY ARCHETYPE ===================")
    for key, title in (("thickness", "FILE THICKNESS"), ("conduct", "PAYMENT CONDUCT"),
                       ("afford", "AFFORDABILITY")):
        print(f"\n  {title}")
        print(f"    {'group':20s} {'n':>5s} {'mean':>7s} {'min':>7s} {'max':>7s} "
              f"{'obs dflt':>9s}")
        g = out.groupby(key)
        for name, sub in sorted(g, key=lambda kv: -kv[1]["score"].mean()):
            act = pd.to_numeric(sub["actual_default"], errors="coerce")
            ad = f"{act.mean():0.3f}" if act.notna().any() else "-"
            print(f"    {str(name):20s} {len(sub):5d} {sub['score'].mean():7.1f} "
                  f"{sub['score'].min():7.1f} {sub['score'].max():7.1f} {ad:>9s}")

    print("\n  Sanity checks to run your eye over:")
    print("    * thick+clean should out-score thin, and thin should out-score arrears;")
    print("    * 'obs dflt' should FALL as the mean score RISES (that is the model")
    print("      working -- though with ~150 people each cell is noisy);")
    print("    * if 'no-file' scores ABOVE 'thick / long clean', missingness is")
    print("      being rewarded and the median-fill is the likely cause.")

    if args.csv:
        os.makedirs(os.path.dirname(os.path.abspath(args.csv)), exist_ok=True)
        out.to_csv(args.csv, index=False)
        print(f"\n  wrote {args.csv}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
