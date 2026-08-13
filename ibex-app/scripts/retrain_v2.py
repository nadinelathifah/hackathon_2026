#!/usr/bin/env python3
"""BUILD 20 -- retrain OB->OB with missingness flags, selection, ensemble.

GOES IN: scripts/retrain_v2.py   (run from the project root)

WHAT THIS IS
------------
The shipped trainer (scripts/calibrate_score.py) feeds ALL parity features in
unscreened. The selection funnel in lgbm_2/run_boost.py was never wired into
production. This joins them, in the OB->OB deployment frame, and adds:

  * missingness flags (obcredit/missingness.py): income_detected, thin_file,
    declared_provided, n_features_missing -- so "we could not measure it" is a
    signal instead of a median-filled lie;
  * permutation importance on VAL, as a second opinion on null importance;
  * an ensemble leg (LightGBM + XGBoost) blended in LOG-ODDS, not rank, because
    rank blending needs a population and so cannot score one applicant;
  * re-derivation of the PD floor from the NEW block table, because the floor is
    data-derived and last run's 0.00314465 is not a constant.

EVERYTHING DOWNSTREAM IS UNCHANGED: isotonic + PAVA, the hybrid tail, the floor
POLICY (lowest block holding at least one observed default), the PDO score map.
Same logic, new numbers.

NO-LEAKAGE PROTOCOL
-------------------
  train (60%) | calib (20%) | eval (20%), chronological by competition week.
  FIT/VAL are carved out of TRAIN by week. ALL selection uses FIT->VAL only.
  Model CHOICE uses CALIB. EVAL is touched once, to report.

WRITES TO --out (default artifacts_v2) so the live Render artifacts are NOT
clobbered until you have read the numbers and promoted them yourself.

EXAMPLE
  py -3.13 scripts/retrain_v2.py "C:/Users/Josep/Downloads/homecredit" --max-cases 0 --cache-dir "C:/Users/Josep/Downloads/obcache" --reuse-ob "C:/Users/Josep/Downloads/obcache/ob_matrix_full_all.pkl" --null-runs 30 --null-pct 95 --nan native --out artifacts_v2
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_LGBM2 = os.path.join(_ROOT, "lgbm_2")
for _p in (_ROOT, _LGBM2, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_compare as rc                                              # noqa: E402
import run_boost as rb                                                # noqa: E402
from obcredit.feature_registry import REGISTRY                        # noqa: E402
from obcredit.missingness import (add_missingness, coverage_report,    # noqa: E402
                                  MISSINGNESS_MONOTONE, MISSINGNESS_NAMES)
from obcredit.modeling.metrics import gini                            # noqa: E402
from obcredit.modeling.calibration import (IsotonicCalibrator,         # noqa: E402
                                           brier_score,
                                           expected_calibration_error,
                                           reliability_table, BASEL_PD_FLOOR)
from obcredit.modeling.scorecard import (pd_to_score, score_to_band,   # noqa: E402
                                         BANDS, DEFAULT_PDO,
                                         DEFAULT_BASE_SCORE,
                                         DEFAULT_BASE_ODDS)


def three_way_split(m, f_train=0.6, f_calib=0.2):
    """Chronological out-of-time split by competition week (as BUILD 18)."""
    sub = m.dropna(subset=[rc.TARGET, rc.WEEKCOL])
    order = list(sub.sort_values(rc.WEEKCOL).index)
    n = len(order)
    i1 = int(n * f_train)
    i2 = int(n * (f_train + f_calib))
    return order[:i1], order[i1:i2], order[i2:]


def prep_x(ref, frame, cols, nan_mode):
    """Feature matrix for `frame`, imputed against `ref` (the training slice).

    nan_mode='median' reproduces run_compare._prep_lgbm exactly (BUILD 18).
    nan_mode='native' leaves NaN in place so LightGBM learns a default direction
    for missing at every split.
    """
    num = frame[cols].apply(pd.to_numeric, errors="coerce")
    if nan_mode == "native":
        return num.to_numpy(dtype=np.float32)
    med = ref[cols].apply(pd.to_numeric, errors="coerce").median(numeric_only=True)
    return num.fillna(med).fillna(0.0).to_numpy(dtype=np.float32)


def train_lgbm(train, valid, cols, monotone, device, nan_mode, params=None,
               seed=42, rounds=3000, stop=100):
    import lightgbm as lgb
    Xtr = prep_x(train, train, cols, nan_mode)
    Xva = prep_x(train, valid, cols, nan_mode)
    ytr = train[rc.TARGET].astype(float).to_numpy()
    yva = valid[rc.TARGET].astype(float).to_numpy()
    p = rc.default_lgbm_params(seed=seed, device=device)
    if params:
        p.update(params)
    pos, neg = float((ytr == 1).sum()), float((ytr == 0).sum())
    p["scale_pos_weight"] = (neg / pos) if pos > 0 else 1.0
    if monotone:
        p["monotone_constraints"] = [int(monotone.get(c, 0)) for c in cols]
    dtr = lgb.Dataset(Xtr, label=ytr, feature_name=list(cols), free_raw_data=False)
    dva = lgb.Dataset(Xva, label=yva, reference=dtr, free_raw_data=False)
    booster = lgb.train(p, dtr, num_boost_round=rounds,
                        valid_sets=[dtr, dva], valid_names=["train", "valid"],
                        callbacks=[lgb.early_stopping(stop, verbose=False),
                                   lgb.log_evaluation(0)])
    return booster, int(booster.best_iteration or rounds)


def permutation_importance(booster, ref, frame, cols, best_it, nan_mode,
                          repeats=3, seed=42):
    """Gini lost when each column is shuffled, measured on VAL (never test).

    Null importance asks 'could this feature's GAIN have arisen from noise?'.
    Permutation importance asks 'does the fitted model actually LOSE accuracy
    without it?'. They disagree on collinear features, which is why both run.
    """
    y = frame[rc.TARGET].astype(float).to_numpy()
    X = prep_x(ref, frame, cols, nan_mode)
    base = float(gini(y, booster.predict(X, num_iteration=best_it)))
    rs = np.random.RandomState(seed)
    out = {}
    for j, c in enumerate(cols):
        drops = []
        for _ in range(max(1, repeats)):
            Xp = X.copy()
            Xp[:, j] = Xp[rs.permutation(len(Xp)), j]
            drops.append(base - float(gini(y, booster.predict(Xp, num_iteration=best_it))))
        out[c] = float(np.mean(drops))
    return base, out


def _logit(p, eps=1e-9):
    p = np.clip(np.asarray(p, dtype=float), eps, 1.0 - eps)
    return np.log(p / (1.0 - p))


def logit_blend(preds, weights=None):
    """Average log-odds, then map back. Unlike rank-averaging this needs no
    reference population, so the SAME function runs for one applicant at serving
    time as for 305k rows at training time."""
    L = np.vstack([_logit(p) for p in preds])
    w = np.ones(len(L)) / len(L) if weights is None else np.asarray(weights, float)
    w = w / w.sum()
    z = np.tensordot(w, L, axes=1)
    return 1.0 / (1.0 + np.exp(-z))


def blocks_of(cal):
    """PAVA blocks: runs of equal fitted PD. Returns list of (start, stop)."""
    y = np.asarray(cal.y_, dtype=float)
    if y.size == 0:
        return []
    cuts = np.r_[0, np.nonzero(np.diff(y) > 1e-15)[0] + 1, len(y)]
    return [(int(cuts[i]), int(cuts[i + 1])) for i in range(len(cuts) - 1)]


def derive_floor(cal, min_defaults=1):
    """POLICY (unchanged from BUILD 19): the floor is the fitted PD of the lowest
    block containing at least `min_defaults` observed defaults.

    Rationale: the rule of three applied to the bottom block gave 0.0075, which
    was HIGHER than the fitted PD of the block above it -- inadmissible, because
    it violated monotonicity and silently reassigned 52 real defaults.
    """
    y = np.asarray(cal.y_, dtype=float)
    n = np.asarray(cal.n_, dtype=float)
    k = np.asarray(cal.k_, dtype=float)
    rows = []
    for bi, (a, b) in enumerate(blocks_of(cal)):
        rows.append({"block": bi, "pd": float(y[a]), "knots": int(b - a),
                     "n": float(n[a:b].sum()), "k": float(k[a:b].sum())})
    chosen = None
    for r in rows:
        if r["k"] >= min_defaults:
            chosen = r
            break
    return chosen, rows


def print_blocks(rows, floor, pdo, base_score, base_odds, limit=8):
    print(f"    {'blk':>4s} {'knots':>7s} {'n':>9s} {'k':>7s} {'pd':>12s} {'score':>8s}")
    for r in rows[:limit]:
        p = max(r["pd"], floor)
        sc = float(pd_to_score(np.asarray([p]), pdo, base_score, base_odds)[0])
        print(f"    {r['block']:4d} {r['knots']:7d} {r['n']:9.0f} {r['k']:7.0f} "
              f"{r['pd']:12.8f} {sc:8.1f}")


def main() -> int:
    ap = argparse.ArgumentParser(description="BUILD 20 retrain (OB->OB).")
    ap.add_argument("kaggle_dir")
    ap.add_argument("--max-cases", type=int, default=0)
    ap.add_argument("--batch", type=int, default=2000)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--cache-dir", default=os.path.join(_LGBM2, "cache"))
    ap.add_argument("--kg-mode", default="raw", choices=["raw", "rendered"])
    ap.add_argument("--reuse-ob", default=None)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--device", default="cpu", choices=["cpu", "gpu"])
    ap.add_argument("--out", default=os.path.join(_ROOT, "artifacts_v2"))
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--nan", dest="nan_mode", default="native",
                    choices=["native", "median"],
                    help="native = LightGBM learns a direction for missing; "
                         "median = reproduce BUILD 18 imputation.")
    ap.add_argument("--no-flags", action="store_true",
                    help="ablation: train without the missingness flags.")
    ap.add_argument("--no-select", action="store_true")
    ap.add_argument("--min-iv", type=float, default=0.005)
    ap.add_argument("--null-runs", type=int, default=30)
    ap.add_argument("--null-rounds", type=int, default=200)
    ap.add_argument("--null-pct", type=float, default=95.0)
    ap.add_argument("--corr-max", type=float, default=0.90)
    ap.add_argument("--perm-repeats", type=int, default=3)
    ap.add_argument("--perm-drop", action="store_true",
                    help="also drop features whose permutation gini drop <= 0.")
    ap.add_argument("--drop", default="declared_education_code",  # BUILD 21 POLICY DROP
                    help="comma-separated features excluded on POLICY grounds before "
                         "any statistical gate (protected-characteristic proxies). "
                         "Pass an empty string to disable.")
    ap.add_argument("--force-keep", default=",".join(MISSINGNESS_NAMES),
                    help="never drop these (comma separated).")
    ap.add_argument("--no-monotone", action="store_true")
    ap.add_argument("--no-xgb", action="store_true")
    ap.add_argument("--pdo", type=float, default=DEFAULT_PDO)
    ap.add_argument("--base-score", type=float, default=DEFAULT_BASE_SCORE)
    ap.add_argument("--base-odds", type=float, default=DEFAULT_BASE_ODDS)
    ap.add_argument("--tail", default="hybrid", choices=["hybrid", "loglinear", "clamp"])
    ap.add_argument("--pd-floor", type=float, default=BASEL_PD_FLOOR,
                    help="provisional floor for the first fit; the FINAL floor is "
                         "re-derived from the new block table unless --keep-floor.")
    ap.add_argument("--keep-floor", action="store_true")
    ap.add_argument("--min-block-defaults", type=int, default=1)
    ap.add_argument("--moc-logodds", type=float, default=0.0)
    ap.add_argument("--no-central-tendency", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    stamp = rc._build_stamp()
    print("=" * 78)
    print("BUILD 20 RETRAIN -- OB->OB, missingness flags, selection, ensemble")
    print(f">>> base: {stamp}")
    print(f">>> nan={args.nan_mode}  flags={'off' if args.no_flags else 'on'}  "
          f"select={'off' if args.no_select else 'on'}  "
          f"monotone={'off' if args.no_monotone else 'on'}")
    print("=" * 78)
    os.makedirs(args.out, exist_ok=True)

    print("\n[1/8] loading OB matrix ...")
    # BUILD 22 REUSE GUARD: never fall back to an in-memory rebuild when a reuse
    # file was requested -- that path holds the whole population in
    # RAM and dies with ArrowMemoryError by design.
    if args.reuse_ob and not os.path.exists(args.reuse_ob):
        raise SystemExit(
            "--reuse-ob file not found: " + str(args.reuse_ob) +
            "\nRun the sharded builder first: scripts\\build_ob_full.py <kaggle_dir> --cache-dir <cache> --resume, then --merge-only.")
    kg, ob = rc.build_or_load_matrices(
        args.kaggle_dir, args.max_cases, args.batch, args.cache_dir, stamp,
        max(1, args.workers), args.kg_mode, rebuild=args.rebuild,
        reuse_ob=args.reuse_ob)
    m = ob
    base_cols = [c for c in REGISTRY.parity_names() if c in m.columns]
    print(f"      rows={len(m):,}  parity features={len(base_cols)}")

    monotone = None if args.no_monotone else dict(REGISTRY.monotone_map())
    if args.no_flags:
        cols_all = list(base_cols)
        print("\n[2/8] missingness flags: SKIPPED (--no-flags ablation)")
    else:
        print("\n[2/8] adding missingness flags (obcredit/missingness.py) ...")
        m = add_missingness(m, base_cols, copy=True)
        flags = []
        for c, mean in coverage_report(m).items():
            const = (mean <= 0.0 or mean >= 1.0) and c != "n_features_missing"
            print(f"      {c:22s} mean={mean:10.4f}"
                  + ("   CONSTANT -> dropped" if const else ""))
            if not const:
                flags.append(c)
        cols_all = list(base_cols) + flags
        if monotone is not None:
            monotone.update(MISSINGNESS_MONOTONE)
    print(f"      feature count: {len(cols_all)}")

    # ---- BUILD 21 POLICY DROP: excluded before any statistical gate ----
    _policy_drop = [c.strip() for c in (args.drop or "").split(",") if c.strip()]
    _dropped_policy = [c for c in _policy_drop if c in cols_all]
    if _dropped_policy:
        cols_all = [c for c in cols_all if c not in set(_dropped_policy)]
        for _c in _dropped_policy:
            monotone.pop(_c, None)
        print("      POLICY DROP (never enters a gate): " + ", ".join(_dropped_policy))
        print("      feature count after policy drop: %d" % len(cols_all))
    _missing_policy = [c for c in _policy_drop if c not in _dropped_policy]
    if _missing_policy:
        print("      NOTE: --drop named features that are not present: " + ", ".join(_missing_policy))


    print("\n[3/8] chronological split by week ...")
    tr_ids, ca_ids, ev_ids = three_way_split(m)
    fit_ids, val_ids = rb.by_week_split(m, tr_ids, frac=0.8)
    train, calib, ev = m.loc[tr_ids], m.loc[ca_ids], m.loc[ev_ids]
    fit_df, val_df = m.loc[fit_ids], m.loc[val_ids]
    print(f"      train={len(train):,} (fit={len(fit_df):,}/val={len(val_df):,})  "
          f"calib={len(calib):,}  eval={len(ev):,}")
    print(f"      base rate train={train[rc.TARGET].mean():0.5f} "
          f"calib={calib[rc.TARGET].mean():0.5f} eval={ev[rc.TARGET].mean():0.5f}")

    forced = {c.strip() for c in args.force_keep.split(",") if c.strip()}
    selected = list(cols_all)
    perm_base, perm = None, {}
    if args.no_select:
        print("\n[4/8] selection: SKIPPED")
    else:
        print("\n[4/8] selection funnel (FIT->VAL only; EVAL never consulted) ...")
        uni = rb.uni_gini(m, selected, fit_ids)
        drop_uni = {c for c in selected
                    if uni.get(c, 0.0) < args.min_iv and c not in forced}
        selected = [c for c in selected if c not in drop_uni]
        print(f"      1) univariate  (|gini|<{args.min_iv}):  "
              f"-{len(drop_uni):<3d} -> {len(selected)} kept")

        if args.null_runs > 0 and len(selected) > 1:
            keep = set(rb.null_importance(m, fit_ids, selected, monotone,
                                          args.device, args))
            drop_null = [c for c in selected if c not in keep and c not in forced]
            selected = [c for c in selected if c in keep or c in forced]
            print(f"      2) null-import ({args.null_runs}x, >{args.null_pct}pct): "
                  f"-{len(drop_null):<3d} -> {len(selected)} kept")
            if drop_null:
                print("         dropped: " + ", ".join(drop_null))
        else:
            print("      2) null-import : skipped")

        before = len(selected)
        clustered = set(rb.spearman_cluster(m, selected, fit_ids, uni, args.corr_max))
        selected = [c for c in selected if c in clustered or c in forced]
        print(f"      3) corr cluster (|rho|>={args.corr_max}):  "
              f"-{before-len(selected):<3d} -> {len(selected)} kept")

        print(f"      4) permutation importance on VAL ({args.perm_repeats}x) ...")
        b_sel, bit_sel = train_lgbm(fit_df, val_df, selected, monotone,
                                    args.device, args.nan_mode, seed=args.seed)
        perm_base, perm = permutation_importance(
            b_sel, fit_df, val_df, selected, bit_sel, args.nan_mode,
            repeats=args.perm_repeats, seed=args.seed)
        ranked = sorted(perm.items(), key=lambda kv: kv[1], reverse=True)
        print(f"         VAL gini (all selected) = {perm_base:0.4f}")
        print(f"         {'feature':32s} {'gini drop':>10s}")
        for c, d in ranked[:12]:
            print(f"         {c:32s} {d:10.5f}")
        if len(ranked) > 12:
            print(f"         ... {len(ranked)-12} more (full list in scorecard.json)")
        dead = [c for c, d in ranked if d <= 0.0 and c not in forced]
        if dead:
            print(f"         {len(dead)} feature(s) with a non-positive drop: "
                  + ", ".join(dead))
        if args.perm_drop and dead:
            selected = [c for c in selected if c not in set(dead)]
            print(f"      5) perm drop:  -{len(dead):<3d} -> {len(selected)} kept")

    if not selected:
        print("      WARNING: selection removed everything; falling back to all features.")
        selected = list(cols_all)
    print(f"      FINAL feature set ({len(selected)}): " + ", ".join(selected))

    print("\n[5/8] training on TRAIN, early stopping + model choice on CALIB ...")
    yca = calib[rc.TARGET].astype(float).to_numpy()
    yev = ev[rc.TARGET].astype(float).to_numpy()

    booster, best = train_lgbm(train, calib, selected, monotone, args.device,
                               args.nan_mode, seed=args.seed)
    lgb_ca = booster.predict(prep_x(train, calib, selected, args.nan_mode),
                             num_iteration=best)
    lgb_ev = booster.predict(prep_x(train, ev, selected, args.nan_mode),
                             num_iteration=best)
    cands = {"lgbm": (lgb_ca, lgb_ev)}
    print(f"      lgbm   best_iter={best:4d}  calib gini={gini(yca, lgb_ca):0.4f}  "
          f"eval gini={gini(yev, lgb_ev):0.4f}")

    xgb_pack = None
    if not args.no_xgb:
        try:
            from step3lib.model_xgb import fit_eval_xgb
            import xgboost as xgb
            xg = fit_eval_xgb(train, calib, selected, target=rc.TARGET,
                              monotone=(monotone or None), seed=args.seed)
            xmed, xbest = xg["medians"], xg["best_iteration"]
            rng = {"iteration_range": (0, int(xbest) + 1)} if xbest is not None else {}

            def _xpred(frame):
                X = frame[selected].apply(pd.to_numeric, errors="coerce")
                X = X.fillna(xmed).fillna(0.0)
                d = xgb.DMatrix(X.to_numpy(dtype=float), feature_names=list(selected))
                return xg["model"].predict(d, **rng)

            xgb_ca, xgb_ev = _xpred(calib), _xpred(ev)
            cands["xgb"] = (xgb_ca, xgb_ev)
            print(f"      xgb    best_iter={int(xbest or 0):4d}  "
                  f"calib gini={gini(yca, xgb_ca):0.4f}  "
                  f"eval gini={gini(yev, xgb_ev):0.4f}")
            cands["blend"] = (logit_blend([lgb_ca, xgb_ca]),
                              logit_blend([lgb_ev, xgb_ev]))
            print(f"      blend  (logit average)   "
                  f"calib gini={gini(yca, cands['blend'][0]):0.4f}  "
                  f"eval gini={gini(yev, cands['blend'][1]):0.4f}")
            xgb_pack = (xg, xmed, xbest)
        except ImportError as e:
            print(f"      xgb skipped (missing wheel: {str(e)[:60]})")
        except Exception as e:
            print(f"      xgb FAILED ({str(e)[:120]})")

    winner = max(cands.keys(), key=lambda k: float(gini(yca, cands[k][0])))
    calib_raw, eval_raw = cands[winner]
    print(f"      --> chosen on CALIB: {winner.upper()}")

    print("\n[6/8] isotonic + PAVA on CALIB, then re-derive the floor ...")

    def _fit_cal(floor_value):
        c = IsotonicCalibrator(tail=args.tail, pd_floor=float(floor_value),
                               break_plateau=True,
                               moc_logodds=float(args.moc_logodds)).fit(calib_raw, yca)
        if not args.no_central_tendency and args.tail != "clamp":
            c.fit_central_tendency(calib_raw, float(yca.mean()))
        return c

    cal = _fit_cal(args.pd_floor)
    chosen, rows = derive_floor(cal, args.min_block_defaults)
    print(f"      knots={len(cal.x_):,}  blocks={len(rows)}  "
          f"calib base rate={float(yca.mean()):0.6f}")
    print("      bottom blocks at the PROVISIONAL floor:")
    print_blocks(rows, float(args.pd_floor), args.pdo, args.base_score, args.base_odds)

    if args.keep_floor or chosen is None:
        floor = float(args.pd_floor)
        print(f"      floor kept as given: {floor:0.8f}")
    else:
        floor = float(chosen["pd"])
        print(f"      POLICY: lowest block with >={args.min_block_defaults} "
              f"observed default(s) is block {chosen['block']} "
              f"(n={chosen['n']:.0f}, k={chosen['k']:.0f})")
        print(f"      re-derived floor: {floor:0.8f}   (provisional was {args.pd_floor:0.8f})")
        cal = _fit_cal(floor)
        chosen, rows = derive_floor(cal, args.min_block_defaults)

    ceiling = float(pd_to_score(np.asarray([floor]), args.pdo,
                                args.base_score, args.base_odds)[0])
    pinned = [r for r in rows if r["pd"] <= floor + 1e-12]
    pin_n = sum(r["n"] for r in pinned)
    pin_k = sum(r["k"] for r in pinned)
    print("      final bottom blocks:")
    print_blocks(rows, floor, args.pdo, args.base_score, args.base_odds)
    print(f"      ceiling score = {ceiling:0.1f}")
    print(f"      ceiling rests on {pin_n:.0f} people, {pin_k:.0f} observed default(s)")
    if pin_k <= 0:
        print("      WARNING: the top block still holds ZERO defaults -- the ceiling")
        print("               is a policy choice, not a measurement. Report it as such.")

    eval_pd = cal.predict(eval_raw)
    diag = cal.diagnose(eval_raw)
    g_raw, g_cal = gini(yev, eval_raw), gini(yev, eval_pd)
    b_raw, b_cal = brier_score(eval_raw, yev), brier_score(eval_pd, yev)
    ece = float(expected_calibration_error(eval_pd, yev))
    print("\n[7/8] ============ EVAL (untouched, out-of-time) ============")
    print(f"      model              : {winner}")
    print(f"      features           : {len(selected)}")
    print(f"      gini raw / calib   : {g_raw:0.4f} / {g_cal:0.4f}")
    print(f"      brier raw -> calib : {b_raw:0.6f} -> {b_cal:0.6f}")
    print(f"      ECE calibrated     : {ece:0.6f}")
    print(f"      pd min / max       : {eval_pd.min():0.6f} / {eval_pd.max():0.6f}")
    print(f"      observed rate      : {float(yev.mean()):0.6f}   "
          f"mean pd {float(eval_pd.mean()):0.6f}")
    print("      reliability (bin: n pred obs):")
    for b, cnt, mp, obsr in reliability_table(eval_pd, yev, n_bins=10):
        if cnt:
            print(f"        {b:2d}: n={cnt:7d}  pred={mp:0.4f}  obs={obsr:0.4f}")
    sc = pd_to_score(eval_pd, args.pdo, args.base_score, args.base_odds)
    bands = np.asarray(score_to_band(sc))
    uniq, counts = np.unique(bands, return_counts=True)
    print("      band mix: " + "  ".join(f"{u}={c}" for u, c in zip(uniq, counts)))
    print(f"      score range: {float(np.min(sc)):0.1f} .. {float(np.max(sc)):0.1f}")
    print(f"      BUILD 18 reference: eval gini 0.4072, ceiling 759.5")

    print(f"\n[8/8] writing artifacts -> {args.out}")
    cal.save(os.path.join(args.out, "calibrator.pkl"))
    booster.save_model(os.path.join(args.out, "model_lgbm.txt"), num_iteration=best)
    if winner == "blend" and xgb_pack is not None:
        xgb_pack[0]["model"].save_model(os.path.join(args.out, "model_xgb.json"))
    _med = train[selected].apply(pd.to_numeric, errors="coerce").median(numeric_only=True)
    medians = {c: (float(_med[c]) if (c in _med.index and pd.notna(_med[c])) else 0.0)
               for c in selected}
    scorecard = {
        "build": "BUILD 20 -- " + stamp,
        "frame": "ob",
        "model": winner,
        "blend": ({"type": "logit_average",
                   "legs": ["model_lgbm.txt", "model_xgb.json"],
                   "weights": [0.5, 0.5]} if winner == "blend" else None),
        "nan_mode": args.nan_mode,
        "features": selected,
        "missingness_flags": [c for c in MISSINGNESS_NAMES if c in selected],
        "medians": medians,
        "monotone": {c: int((monotone or {}).get(c, 0)) for c in selected},
        "best_iteration": int(best),
        "xgb_best_iteration": (int(xgb_pack[2]) if (winner == "blend" and xgb_pack
                                                    and xgb_pack[2] is not None) else None),
        "pdo": args.pdo, "base_score": args.base_score, "base_odds": args.base_odds,
        "bands": [[None if c == float("-inf") else c, l] for c, l in BANDS],
        "eval_gini": float(g_cal), "eval_gini_raw": float(g_raw),
        "eval_brier_raw": float(b_raw), "eval_brier_calibrated": float(b_cal),
        "eval_ece_calibrated": ece,
        "selection": {
            "candidates": len(cols_all), "kept": len(selected),
            "min_iv": args.min_iv, "null_runs": args.null_runs,
            "null_pct": args.null_pct, "corr_max": args.corr_max,
            "perm_val_gini": perm_base, "perm_gini_drop": perm,
            "forced_keep": sorted(forced),
        },
        "calibration": {
            "tail": args.tail, "pd_floor": floor,
            "pd_floor_provisional": float(args.pd_floor),
            "floor_policy": ("lowest block with >=%d observed default(s)"
                             % args.min_block_defaults
                             if not args.keep_floor else "fixed by --pd-floor"),
            "ceiling_score": ceiling,
            "ceiling_rests_on_obs": float(pin_n),
            "ceiling_rests_on_defaults": float(pin_k),
            "blocks": len(rows),
            "moc_logodds": float(args.moc_logodds),
            "central_tendency_applied": (not args.no_central_tendency),
            "observed_default_rate": float(yev.mean()),
            "diagnostics": {k: (float(v) if isinstance(v, (int, float)) else v)
                            for k, v in diag.items()},
        },
        "block_table": rows[:40],
    }
    with open(os.path.join(args.out, "scorecard.json"), "w", encoding="utf-8") as f:
        json.dump(scorecard, f, indent=2)
    print("      wrote calibrator.pkl, model_lgbm.txt"
          + (", model_xgb.json" if (winner == "blend" and xgb_pack) else "")
          + ", scorecard.json")
    print(f"\nDONE in {(time.time()-t0)/60.0:0.1f} min")
    print("NEXT: compare eval_gini against 0.4072 (BUILD 18), then re-run")
    print(f"      scripts/evidence_se.py with --floor {floor:0.8f} for the CIs.")
    print("      Artifacts are in a SEPARATE folder -- promote them yourself.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
