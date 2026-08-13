#!/usr/bin/env python3
"""BUILD 14 -- calibrate the model and produce a defensible score.

Out-of-time pipeline (no leakage):
  1) build/load the feature matrices (reuses run_compare.py's builder -> the
     SAME f() and SAME columns as the comparison harness);
  2) 3-way chronological split by competition week: train | calib | eval;
  3) fit LightGBM on train (same params + monotone constraints as run_compare);
  4) fit isotonic calibration on calib (raw score -> real PD);
  5) report Gini (ranking preserved), Brier raw-vs-calibrated and a reliability
     table on the UNTOUCHED eval slice;
  6) turn calibrated PD into a PDO credit score + A-E band, print samples;
  7) persist artifacts/calibrator.pkl, artifacts/model_lgbm.txt, scorecard.json
     for the serving backend.

Run from the project root, e.g.:
  python scripts/calibrate_score.py "C:\\Users\\Josep\\Downloads\\homecredit" \\
        --max-cases 0 --cache-dir "C:\\Users\\Josep\\Downloads\\obcache"
"""
from __future__ import annotations
import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_LGBM2 = os.path.join(_ROOT, "lgbm_2")
for _p in (_ROOT, _LGBM2):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_compare as rc  # noqa: E402
from obcredit.feature_registry import REGISTRY  # noqa: E402
from obcredit.modeling.metrics import gini  # noqa: E402
from obcredit.modeling.calibration import (IsotonicCalibrator, brier_score,  # noqa: E402
                                           expected_calibration_error,
                                           reliability_table, BASEL_PD_FLOOR)
from obcredit.modeling.scorecard import (pd_to_score, score_to_band, BANDS,  # noqa: E402
                                         DEFAULT_PDO, DEFAULT_BASE_SCORE,
                                         DEFAULT_BASE_ODDS)


def three_way_split(m, f_train=0.6, f_calib=0.2):
    """Chronological (out-of-time) split by competition week."""
    sub = m.dropna(subset=[rc.TARGET, rc.WEEKCOL])
    order = list(sub.sort_values(rc.WEEKCOL).index)
    n = len(order)
    i1 = int(n * f_train)
    i2 = int(n * (f_train + f_calib))
    return order[:i1], order[i1:i2], order[i2:]


def main() -> int:
    ap = argparse.ArgumentParser(description="Calibrate + score (BUILD 14).")
    ap.add_argument("kaggle_dir")
    ap.add_argument("--frame", choices=["ob", "kg"], default="ob",
                    help="representation to deploy on (default ob = open banking).")
    ap.add_argument("--max-cases", type=int, default=0)
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--cache-dir", default=os.path.join(_LGBM2, "cache"))
    ap.add_argument("--kg-mode", default="raw", choices=["raw", "rendered"])
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--reuse-ob", default=None)
    ap.add_argument("--pdo", type=float, default=DEFAULT_PDO)
    ap.add_argument("--base-score", type=float, default=DEFAULT_BASE_SCORE)
    ap.add_argument("--base-odds", type=float, default=DEFAULT_BASE_ODDS)
    ap.add_argument("--out", default=os.path.join(_ROOT, "artifacts"))
    # ---- BUILD 18 calibration controls ----
    ap.add_argument("--tail", default="hybrid",
                    choices=["hybrid", "loglinear", "clamp"],
                    help="how to price where isotonic has no information.")
    ap.add_argument("--pd-floor", type=float, default=BASEL_PD_FLOOR,
                    help="regulatory PD floor (CRR Art.160/163 = 0.0003).")
    ap.add_argument("--moc-logodds", type=float, default=0.0,
                    help="Margin of Conservatism, in log-odds, applied ONLY "
                         "where PD is extrapolated rather than observed.")
    ap.add_argument("--no-break-plateau", action="store_true",
                    help="do not let the backbone override a terminal isotonic "
                         "plateau (reproduces BUILD 17).")
    ap.add_argument("--no-central-tendency", action="store_true",
                    help="skip re-anchoring mean predicted PD to the observed "
                         "default rate.")
    args = ap.parse_args()

    import lightgbm as lgb

    stamp = rc._build_stamp()
    print(stamp)
    os.makedirs(args.out, exist_ok=True)

    kg, ob = rc.build_or_load_matrices(
        args.kaggle_dir, args.max_cases, args.batch, args.cache_dir, stamp,
        args.workers, args.kg_mode, rebuild=args.rebuild, reuse_ob=args.reuse_ob)
    m = ob if args.frame == "ob" else kg
    print(f"[calibrate] frame={args.frame}  rows={len(m):,}")

    monotone = REGISTRY.monotone_map()
    cols = [c for c in REGISTRY.parity_names() if c in m.columns]
    print(f"[calibrate] using {len(cols)} parity features")
    for c in ("declared_is_homeowner", "declared_income_gap"):
        if c in m.columns:
            frac = float(pd.to_numeric(m[c], errors="coerce").notna().mean())
            print(f"[calibrate] coverage {c}: {frac:0.1%}")

    tr_ids, ca_ids, ev_ids = three_way_split(m)
    train, calib, ev = m.loc[tr_ids], m.loc[ca_ids], m.loc[ev_ids]
    print(f"[calibrate] split train={len(train):,} calib={len(calib):,} eval={len(ev):,}")

    Xtr, Xca = rc._prep_lgbm(train, calib, cols)
    _, Xev = rc._prep_lgbm(train, ev, cols)
    ytr = train[rc.TARGET].astype(float).to_numpy()
    yca = calib[rc.TARGET].astype(float).to_numpy()
    yev = ev[rc.TARGET].astype(float).to_numpy()

    params = rc.default_lgbm_params(seed=42, device=args.device)
    pos, neg = float((ytr == 1).sum()), float((ytr == 0).sum())
    params["scale_pos_weight"] = (neg / pos) if pos > 0 else 1.0
    params["monotone_constraints"] = [int(monotone.get(c, 0)) for c in cols]

    dtr = lgb.Dataset(Xtr.to_numpy(np.float32), label=ytr,
                      feature_name=list(cols), free_raw_data=False)
    dca = lgb.Dataset(Xca.to_numpy(np.float32), label=yca, reference=dtr,
                      free_raw_data=False)
    booster = lgb.train(params, dtr, num_boost_round=3000,
                        valid_sets=[dtr, dca], valid_names=["train", "calib"],
                        callbacks=[lgb.early_stopping(100, verbose=False),
                                   lgb.log_evaluation(0)])
    best = booster.best_iteration or 3000

    calib_raw = booster.predict(Xca.to_numpy(np.float32), num_iteration=best)
    eval_raw = booster.predict(Xev.to_numpy(np.float32), num_iteration=best)

    # ---------------- BUILD 18: choose the tail treatment BY MEASUREMENT ----
    # Breaking the isotonic plateau de-caps the credit score, but it also moves
    # the PD, so it must be justified on held-out calibration quality rather
    # than asserted. Fit all three and print the comparison.
    print("\n========== tail treatment comparison (EVAL, out-of-time) ==========")
    print("  mode        brier      ECE      gini     pd_min    pd_max   %extrap")
    bake = {}
    for mode in ("clamp", "loglinear", "hybrid"):
        c = IsotonicCalibrator(
            tail=mode,
            pd_floor=(0.0 if mode == "clamp" else float(args.pd_floor)),
            break_plateau=not args.no_break_plateau,
            moc_logodds=float(args.moc_logodds)).fit(calib_raw, yca)
        if not args.no_central_tendency and mode != "clamp":
            c.fit_central_tendency(calib_raw, float(yca.mean()))
        p = c.predict(eval_raw)
        d = c.diagnose(eval_raw)
        bake[mode] = (c, p, d)
        print(f"  {mode:10s} {brier_score(p, yev):0.6f} "
              f"{expected_calibration_error(p, yev):0.6f} "
              f"{gini(yev, p):0.4f}  {p.min():0.6f}  {p.max():0.6f}  "
              f"{100.0 * d['frac_backbone_priced']:5.1f}%")
    print("  NOTE: gini should be flat across modes (monotone maps cannot")
    print("        reorder). Any movement is isotonic tie-breaking, not skill.")
    print("        Adopt hybrid ONLY if its brier/ECE are not worse than")
    print("        loglinear -- otherwise the plateau was real, and capping")
    print("        the score at ~590 is the honest answer.")

    cal, eval_pd, diag = bake[args.tail]

    print("\n  calibrator diagnostics (%s):" % args.tail)
    for k in ("frac_backbone_priced", "frac_below_support", "frac_above_support",
              "lower_plateau_knots", "upper_plateau_knots", "lower_plateau_z",
              "isotonic_pd_min", "pd_min", "pd_max", "mean_pd", "backbone_slope",
              "ct_shift", "moc_logodds", "break_plateau"):
        print(f"    {k:22s} {diag[k]}")
    print(f"    {'observed_default_rate':22s} {float(yev.mean()):0.6f}")
    print(f"    {'score at pd_min':22s} "
          f"{float(pd_to_score(diag['pd_min'], args.pdo, args.base_score, args.base_odds)):0.1f}")

    g_raw = gini(yev, eval_raw)
    g_cal = gini(yev, eval_pd)
    b_raw = brier_score(eval_raw, yev)   # raw score is not a probability
    b_cal = brier_score(eval_pd, yev)
    print("\n================= EVAL (untouched, out-of-time) =================")
    print(f"  best_iteration     : {best}")
    print(f"  tail treatment     : {args.tail}")
    print(f"  gini raw / calib   : {g_raw:0.4f} / {g_cal:0.4f}  (ranking preserved)")
    print(f"  brier raw -> calib : {b_raw:0.4f} -> {b_cal:0.4f}")
    print(f"  ECE calibrated     : {expected_calibration_error(eval_pd, yev):0.6f}")
    print("  reliability (bin: count mean_pred observed):")
    for b, cnt, mp, obsr in reliability_table(eval_pd, yev, n_bins=10):
        if cnt:
            print(f"    {b:2d}: n={cnt:6d}  pred={mp:0.3f}  obs={obsr:0.3f}")

    scores = pd_to_score(eval_pd, args.pdo, args.base_score, args.base_odds)
    bands = score_to_band(scores)
    print("\n  sample scored applicants:")
    print("    case_id           PD     score  band")
    for cid, pdv, sc, bd in list(zip(ev.index, eval_pd, scores, bands))[:10]:
        print(f"    {str(cid)[:14]:14s}  {pdv:0.3f}  {sc:6.1f}  {bd}")

    cal.save(os.path.join(args.out, "calibrator.pkl"))
    booster.save_model(os.path.join(args.out, "model_lgbm.txt"), num_iteration=best)
    # persist the training-slice medians so the serving backend imputes missing
    # features EXACTLY like _prep_lgbm did at train time (median, then 0.0).
    _med = train[cols].median(numeric_only=True)
    medians = {c: (float(_med[c]) if (c in _med.index and pd.notna(_med[c])) else 0.0)
               for c in cols}
    scorecard = {
        "build": stamp,
        "frame": args.frame,
        "features": cols,
        "medians": medians,
        "monotone": {c: int(monotone.get(c, 0)) for c in cols},
        "best_iteration": int(best),
        "pdo": args.pdo,
        "base_score": args.base_score,
        "base_odds": args.base_odds,
        "bands": [[None if cutoff == float("-inf") else cutoff, label]
                  for cutoff, label in BANDS],
        "eval_gini": g_cal,
        "eval_brier_raw": b_raw,
        "eval_brier_calibrated": b_cal,
        "eval_ece_calibrated": float(expected_calibration_error(eval_pd, yev)),
        "calibration": {
            "tail": args.tail,
            "pd_floor": float(args.pd_floor),
            "moc_logodds": float(args.moc_logodds),
            "break_plateau": (not args.no_break_plateau),
            "central_tendency_applied": (not args.no_central_tendency),
            "observed_default_rate": float(yev.mean()),
            "diagnostics": {k: (float(v) if isinstance(v, (int, float)) else v)
                            for k, v in diag.items()},
        },
    }
    with open(os.path.join(args.out, "scorecard.json"), "w", encoding="utf-8") as f:
        json.dump(scorecard, f, indent=2)
    print(f"\n[calibrate] wrote calibrator.pkl, model_lgbm.txt, scorecard.json -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
