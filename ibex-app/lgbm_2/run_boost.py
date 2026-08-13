#!/usr/bin/env python3
"""LGBM 2 -- PUSH GINI HIGHER (selection -> tuning -> ensemble).

This does NOT rebuild anything: it reuses the KG + OB matrices that run_compare.py
already cached (or --reuse-ob), then layers the accuracy/stability steps we
sequenced on top -- all evaluated in the KG->OB DEPLOYMENT frame (train on clean
Kaggle-direct signal, serve on the open-banking reconstruction), because that is
the number that matters for the dissertation.

PIPELINE (each stage reports KG->OB test Gini + competition stability, so you see
the climb):
  0. baseline    -- LightGBM, ALL shared parity features, default params
  1. + selection -- a 4-gate funnel prunes noisy / unreliable / redundant features
  2. + tuning    -- random search over LightGBM params, scored by STABILITY
  3. + ensemble  -- rank-average blend of the tuned LightGBM and XGBoost

NO-LEAKAGE PROTOCOL (this is the defensible bit):
  * shared_split -> train_ids / test_ids by week (out-of-time). TEST is touched
    ONLY to score the final number of each stage.
  * train_ids is further split by week into FIT / VAL. ALL selection and ALL
    tuning decisions use FIT -> VAL only. The test fold never informs a feature
    choice, an early-stopping round, or a hyper-parameter.

SELECTION FUNNEL (in order; each gate logged with the count it removes):
  1. drift gate      -- drop features whose train(KG)->serve(OB) distribution
                        shift is too large (|mean_KG-mean_OB|/std_KG > --drift-max);
                        these reconstruct badly and hurt KG->OB at serving time.
  2. univariate gate -- drop features with |Gini| < --min-iv on FIT (no signal).
  3. null-importance -- fit LightGBM on FIT, compare each feature's gain to the
                        gain it gets under --null-runs target shuffles; keep only
                        features beating the --null-pct percentile of their own
                        noise. (This is the honest way to kill spurious gain.)
  4. corr cluster    -- Spearman-cluster survivors at |rho| >= --corr-max and keep
                        the strongest (by univariate Gini) per cluster.

HONEST EXPECTATION: these are MODEL-SIDE gains -- typically a few Gini points and
a firmer stability slope. The big lever for KG->OB remains reconstruction FIDELITY
(better renderers + more reconstructable source tables); that is separate work.

Requires numpy + pandas + lightgbm (+ xgboost for the ensemble stage).
Must live INSIDE the step3 project root (next to obcredit/, step3lib/, scripts/).

EXAMPLES
  # reuse the OB matrix you already built; build the (cheap) KG side once, then boost
  python lgbm_2/run_boost.py "C:\\Users\\Josep\\Downloads\\homecredit" --max-cases 0 \
        --reuse-ob ..\\lgbm_test_run\\cache\\ob_matrix_full.pkl

  # faster iteration: lighter tuning / null search
  python lgbm_2/run_boost.py "...\\homecredit" --max-cases 0 --tune-iter 15 --null-runs 10

  # skip stages
  python lgbm_2/run_boost.py "...\\homecredit" --max-cases 0 --no-tune --null-runs 0
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
for _p in (_ROOT, os.path.join(_ROOT, "scripts"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_compare as RC                                              # noqa: E402
from run_compare import (                                            # noqa: E402
    TARGET, WEEKCOL, build_or_load_matrices, shared_split, drift_report,
    default_lgbm_params, _prep_lgbm, _probe_gpu, _build_stamp,
)
from obcredit.feature_registry import REGISTRY                       # noqa: E402
from obcredit.modeling.metrics import gini, gini_stability           # noqa: E402


# --------------------------------------------------------------------------- #
# Small, pure helpers (no lightgbm import -> unit-testable without the wheel)
# --------------------------------------------------------------------------- #
def by_week_split(mat, ids, frac=0.8):
    """Split ids into (fit, val) out-of-time by WEEKCOL. Earlier weeks -> fit."""
    sub = mat.loc[ids, [WEEKCOL]].dropna(subset=[WEEKCOL])
    order = sub.sort_values(WEEKCOL).index
    cut = max(1, int(len(order) * frac))
    return list(order[:cut]), list(order[cut:])


def uni_gini(frame, cols, ids):
    """Absolute univariate Gini of each feature vs TARGET on `ids` (FIT only)."""
    y = frame.loc[ids, TARGET].to_numpy(dtype=float)
    out = {}
    for c in cols:
        x = pd.to_numeric(frame.loc[ids, c], errors="coerce").to_numpy(dtype=float)
        try:
            out[c] = abs(gini(y, x))
        except Exception:
            out[c] = 0.0
    return out


def spearman_cluster(frame, cols, ids, uni, corr_max):
    """Greedy Spearman de-duplication: keep the strongest feature per correlated
    cluster. Spearman = Pearson on ranks."""
    if len(cols) <= 1:
        return list(cols)
    sub = frame.loc[ids, cols].apply(pd.to_numeric, errors="coerce")
    corr = sub.rank().corr()
    kept = []
    for c in sorted(cols, key=lambda k: uni.get(k, 0.0), reverse=True):
        ok = True
        for k in kept:
            r = corr.loc[c, k]
            if not np.isnan(r) and abs(r) >= corr_max:
                ok = False
                break
        if ok:
            kept.append(c)
    return kept


def score(test_df, pred):
    """(test Gini, competition stability metric) for predictions on test_df."""
    y = test_df[TARGET].to_numpy(dtype=float)
    w = test_df[WEEKCOL].to_numpy(dtype=float)
    g = float(gini(y, pred))
    s = gini_stability(w, y, pred)
    return g, float(s.get("metric") or 0.0)


def rank_blend(preds):
    """Rank-average an iterable of equally-ordered prediction arrays."""
    ranks = [pd.Series(p).rank().to_numpy() for p in preds]
    return np.mean(ranks, axis=0)


# --------------------------------------------------------------------------- #
# LightGBM fit / predict (import inside so the module loads without the wheel)
# --------------------------------------------------------------------------- #
def lgbm_fit(fit_df, val_df, cols, params=None, monotone=None, device="cpu",
             num_boost_round=3000, early_stopping_rounds=80, seed=42):
    """Fit on fit_df; if val_df is given, early-stop on it. Returns (booster, best_it).
    val_df=None -> train a fixed `num_boost_round` with no early stopping (used for
    null-importance so the real target's val signal can't leak into the count)."""
    import lightgbm as lgb
    ytr = fit_df[TARGET].astype(float).to_numpy()
    p = default_lgbm_params(seed=seed, device=device)
    if params:
        p.update(params)
    pos, neg = float((ytr == 1).sum()), float((ytr == 0).sum())
    p["scale_pos_weight"] = (neg / pos) if pos > 0 else 1.0
    if monotone:
        p["monotone_constraints"] = [int(monotone.get(c, 0)) for c in cols]
    if val_df is not None and early_stopping_rounds > 0:
        tr, va = _prep_lgbm(fit_df, val_df, cols)
        dtr = lgb.Dataset(tr.to_numpy(np.float32), label=ytr, feature_name=list(cols), free_raw_data=False)
        dva = lgb.Dataset(va.to_numpy(np.float32), label=val_df[TARGET].astype(float).to_numpy(),
                          reference=dtr, free_raw_data=False)
        booster = lgb.train(p, dtr, num_boost_round=num_boost_round,
                            valid_sets=[dva], valid_names=["val"],
                            callbacks=[lgb.early_stopping(early_stopping_rounds, verbose=False),
                                       lgb.log_evaluation(0)])
        best = booster.best_iteration or num_boost_round
    else:
        tr = fit_df[cols].fillna(fit_df[cols].median(numeric_only=True)).fillna(0.0)
        dtr = lgb.Dataset(tr.to_numpy(np.float32), label=ytr, feature_name=list(cols), free_raw_data=False)
        booster = lgb.train(p, dtr, num_boost_round=num_boost_round,
                            callbacks=[lgb.log_evaluation(0)])
        best = num_boost_round
    return booster, int(best)


def lgbm_pred(booster, ref_fit_df, target_df, cols, best_it):
    """Predict target_df, imputing with ref_fit_df (train) medians -- no leakage."""
    _, te = _prep_lgbm(ref_fit_df, target_df, cols)
    return booster.predict(te.to_numpy(np.float32), num_iteration=best_it)


# --------------------------------------------------------------------------- #
# Selection funnel (FIT -> VAL only)
# --------------------------------------------------------------------------- #
def select_features(train_mat, kg, ob, fit_ids, val_ids, cols, monotone, device, args, cross_rep):
    uni = uni_gini(train_mat, cols, fit_ids)
    surv = list(cols)
    print(f"      start: {len(surv)} shared parity features")

    # 1) cross-representation FIDELITY gates -- only meaningful when the training
    #    representation differs from serving (KG->OB). For OB->OB / KG->KG train
    #    and serve share a distribution, so there is nothing to gate here.
    if cross_rep:
        dr = drift_report(kg.loc[fit_ids], ob.loc[fit_ids], surv, top=len(surv))
        # 1a) drift gate -- large mean shift between train(KG) and serve(OB)
        drop_drift = set(dr.loc[dr["std_shift"] > args.drift_max, "feature"].tolist())
        surv = [c for c in surv if c not in drop_drift]
        print(f"      1a) drift gate    (std_shift>{args.drift_max}):    -{len(drop_drift):<3d} -> {len(surv)} kept")
        # 1b) fidelity gate -- low per-applicant KG<->OB correlation means the
        #     feature does not reconstruct (scrambled at serving); drop it.
        fmap = {row["feature"]: row["corr"] for _, row in dr.iterrows()}
        drop_fid = {c for c in surv
                    if (fmap.get(c) is None or np.isnan(fmap.get(c, np.nan))
                        or fmap.get(c, 0.0) < args.min_fidelity_corr)}
        surv = [c for c in surv if c not in drop_fid]
        print(f"      1b) fidelity gate (KG<->OB corr<{args.min_fidelity_corr}): -{len(drop_fid):<3d} -> {len(surv)} kept")
    else:
        print("      1) fidelity gates : skipped (train and serve share one representation)")

    # 2) univariate signal gate
    drop_uni = {c for c in surv if uni.get(c, 0.0) < args.min_iv}
    surv = [c for c in surv if c not in drop_uni]
    print(f"      2) univariate    (|gini|<{args.min_iv}):      -{len(drop_uni):<3d} -> {len(surv)} kept")

    # 3) null-importance
    if args.null_runs > 0 and len(surv) > 1:
        keep = null_importance(train_mat, fit_ids, surv, monotone, device, args)
        drop_null = [c for c in surv if c not in keep]
        surv = [c for c in surv if c in keep]
        print(f"      3) null-import   ({args.null_runs}x, >{args.null_pct}pct): -{len(drop_null):<3d} -> {len(surv)} kept")
    else:
        print("      3) null-import   : skipped")

    # 4) Spearman correlation clustering
    before = len(surv)
    surv = spearman_cluster(train_mat, surv, fit_ids, uni, args.corr_max)
    print(f"      4) corr cluster  (|rho|>={args.corr_max}):     -{before-len(surv):<3d} -> {len(surv)} kept")
    return surv


def null_importance(kg, fit_ids, cols, monotone, device, args):
    """Keep features whose real gain beats the --null-pct percentile of the gain
    they earn under target shuffles. Fixed rounds, no early stopping (clean)."""
    fit_df = kg.loc[fit_ids]
    booster, _ = lgbm_fit(fit_df, None, cols, monotone=monotone, device=device,
                          num_boost_round=args.null_rounds, seed=args.seed)
    real = np.asarray(booster.feature_importance(importance_type="gain"), dtype=float)
    rs = np.random.RandomState(args.seed)
    null = np.zeros((args.null_runs, len(cols)), dtype=float)
    y = fit_df[TARGET].astype(float).to_numpy()
    for k in range(args.null_runs):
        yk = y.copy()
        rs.shuffle(yk)
        shuffled = fit_df.copy()
        shuffled[TARGET] = yk
        bk, _ = lgbm_fit(shuffled, None, cols, monotone=monotone, device=device,
                         num_boost_round=args.null_rounds, seed=args.seed)
        null[k] = np.asarray(bk.feature_importance(importance_type="gain"), dtype=float)
        print(f"         null-importance shuffle {k+1}/{args.null_runs}", flush=True)
    thr = np.percentile(null, args.null_pct, axis=0)
    return [c for i, c in enumerate(cols) if real[i] > thr[i]]


# --------------------------------------------------------------------------- #
# Tuning (FIT -> VAL, scored by STABILITY -- the competition target)
# --------------------------------------------------------------------------- #
def _sample_params(rs):
    return {
        "learning_rate": float(rs.choice([0.02, 0.03, 0.05])),
        "num_leaves": int(rs.choice([15, 31, 63, 127])),
        "max_depth": int(rs.choice([-1, 4, 6, 8])),
        "min_child_samples": int(rs.choice([20, 50, 100, 200])),
        "feature_fraction": float(rs.choice([0.6, 0.7, 0.8, 0.9])),
        "bagging_fraction": float(rs.choice([0.6, 0.7, 0.8, 0.9])),
        "lambda_l1": float(rs.choice([0.0, 0.5, 1.0, 2.0, 5.0])),
        "lambda_l2": float(rs.choice([0.0, 1.0, 2.0, 5.0, 10.0])),
    }


def tune(train_mat, fit_ids, val_ids, cols, monotone, device, args):
    fit_df, val_df = train_mat.loc[fit_ids], train_mat.loc[val_ids]
    rs = np.random.RandomState(args.seed)
    best_params, best_stab, best_g = None, -1e9, -1e9
    for it in range(args.tune_iter):
        params = _sample_params(rs)
        booster, best_it = lgbm_fit(fit_df, val_df, cols, params=params, monotone=monotone,
                                    device=device, num_boost_round=1500, early_stopping_rounds=60,
                                    seed=args.seed)
        pv = lgbm_pred(booster, fit_df, val_df, cols, best_it)
        g, s = score(val_df, pv)
        tag = ""
        if s > best_stab:
            best_stab, best_g, best_params, tag = s, g, params, "  <-- best"
        print(f"         tune {it+1:>2}/{args.tune_iter}: val stability={s:0.4f} gini={g:0.4f}{tag}", flush=True)
    print(f"      best tuned params: {best_params}")
    return best_params


# --------------------------------------------------------------------------- #
# Stage evaluator: train KG(fit) -> serve OB(test), early-stop on KG(val).
# --------------------------------------------------------------------------- #
def eval_stage(train_mat, serve_mat, fit_ids, val_ids, test_ids, cols, params, monotone, device):
    fit_df, val_df, test_df = train_mat.loc[fit_ids], train_mat.loc[val_ids], serve_mat.loc[test_ids]
    booster, best_it = lgbm_fit(fit_df, val_df, cols, params=params, monotone=monotone, device=device)
    pte = lgbm_pred(booster, fit_df, test_df, cols, best_it)
    g, s = score(test_df, pte)
    return g, s, pte


def main() -> int:
    ap = argparse.ArgumentParser(description="Push KG->OB Gini higher: selection -> tuning -> ensemble.")
    ap.add_argument("kaggle_dir")
    ap.add_argument("--max-cases", type=int, default=25000, help="0 = full population")
    ap.add_argument("--batch", type=int, default=3000)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--kg-mode", choices=["raw", "rendered"], default="raw")
    ap.add_argument("--reuse-ob", default=None, help="path to an existing ob_matrix_*.pkl to reuse")
    ap.add_argument("--cache-dir", default=os.path.join(_HERE, "cache"))
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--device", choices=["cpu", "gpu"], default="cpu", help="LightGBM training device")
    ap.add_argument("--no-monotone", action="store_true")
    ap.add_argument("--frame", choices=["kg2ob", "ob2ob", "kg2kg"], default="kg2ob",
                    help="train->serve representation: kg2ob (diagnostic), ob2ob (deployable/what you ship), kg2kg (clean ceiling)")
    # selection knobs
    ap.add_argument("--drift-max", type=float, default=3.0, help="drop features with std_shift above this (cross-rep only)")
    ap.add_argument("--min-fidelity-corr", type=float, default=0.30,
                    help="drop features whose per-applicant KG<->OB correlation is below this (cross-rep only)")
    ap.add_argument("--min-iv", type=float, default=0.005, help="drop features with |univariate gini| below this")
    ap.add_argument("--null-runs", type=int, default=15, help="target shuffles for null-importance (0 = skip)")
    ap.add_argument("--null-rounds", type=int, default=200, help="boosting rounds per null-importance fit")
    ap.add_argument("--null-pct", type=float, default=75.0, help="percentile of null gain a feature must beat")
    ap.add_argument("--corr-max", type=float, default=0.90, help="Spearman |rho| clustering threshold")
    # tuning knobs
    ap.add_argument("--tune-iter", type=int, default=25, help="random-search iterations (0 or --no-tune to skip)")
    ap.add_argument("--no-tune", action="store_true")
    ap.add_argument("--no-ensemble", action="store_true", help="skip the LGBM+XGB blend stage")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    FR = {"kg2ob": "KG->OB", "ob2ob": "OB->OB", "kg2kg": "KG->KG"}[args.frame]
    cross_rep = args.frame == "kg2ob"

    if not os.path.isdir(args.kaggle_dir):
        print(f"ERROR: not a directory: {args.kaggle_dir}", file=sys.stderr)
        return 2

    device = args.device
    stamp = _build_stamp()
    print("=" * 80)
    print(f"LGBM 2: PUSH GINI HIGHER  (selection -> tuning -> ensemble; frame={FR})")
    print(f">>> RUNNING: {stamp}")
    print(f">>> monotone={'off' if args.no_monotone else 'on'} | lgbm_device={device} | "
          f"workers={args.workers} | kg_mode={args.kg_mode}")
    print("=" * 80)
    if device == "gpu" and not _probe_gpu():
        device = "cpu"

    print("\n[1/6] building / loading KG + OB matrices (reuses cache; no rebuild if present) ...")
    kg, ob = build_or_load_matrices(args.kaggle_dir, args.max_cases, args.batch,
                                    args.cache_dir, stamp, max(1, args.workers), args.kg_mode,
                                    rebuild=args.rebuild, reuse_ob=args.reuse_ob)
    print(f"      KG {kg.shape}   OB {ob.shape}")

    cols_all = [c for c in REGISTRY.parity_names() if c in kg.columns and c in ob.columns]
    monotone = None if args.no_monotone else REGISTRY.monotone_map()

    # Resolve the train/serve representation from --frame.
    train_mat = kg if args.frame in ("kg2ob", "kg2kg") else ob
    serve_mat = ob if args.frame in ("kg2ob", "ob2ob") else kg
    print(f"      frame={FR}: train on {'KG' if train_mat is kg else 'OB'}, "
          f"serve on {'OB' if serve_mat is ob else 'KG'}"
          f"{'  (cross-representation)' if cross_rep else '  (matched representation)'}")

    print("\n[2/6] shared out-of-time split, then FIT/VAL carved from TRAIN ...")
    train_ids, test_ids = shared_split(kg, ob)
    fit_ids, val_ids = by_week_split(train_mat, train_ids, frac=0.8)
    print(f"      train={len(train_ids):,} (fit={len(fit_ids):,} / val={len(val_ids):,})  test={len(test_ids):,}")

    progression = []

    print("\n[3/6] BASELINE: LightGBM, all features, default params ...")
    g0, s0, _ = eval_stage(train_mat, serve_mat, fit_ids, val_ids, test_ids, cols_all, None, monotone, device)
    progression.append(("0 baseline (all feats)", len(cols_all), g0, s0))
    print(f"      {FR}  gini={g0:0.4f}  stability={s0:0.4f}")

    print("\n[4/6] SELECTION funnel (FIT->VAL only) ...")
    selected = select_features(train_mat, kg, ob, fit_ids, val_ids, cols_all, monotone, device, args, cross_rep)
    if not selected:
        print("      WARNING: selection removed everything; falling back to all features.")
        selected = cols_all
    g1, s1, _ = eval_stage(train_mat, serve_mat, fit_ids, val_ids, test_ids, selected, None, monotone, device)
    progression.append(("1 + selection", len(selected), g1, s1))
    print(f"      selected {len(selected)} features -> {FR} gini={g1:0.4f}  stability={s1:0.4f}")
    print("      kept: " + ", ".join(selected))

    best_params = None
    if args.no_tune or args.tune_iter <= 0:
        print("\n[5/6] TUNING: skipped")
        g2, s2, pte_lgb = g1, s1, None
    else:
        print(f"\n[5/6] TUNING: {args.tune_iter}-iter random search on selected features (scored by stability) ...")
        best_params = tune(train_mat, fit_ids, val_ids, selected, monotone, device, args)
        g2, s2, pte_lgb = eval_stage(train_mat, serve_mat, fit_ids, val_ids, test_ids, selected, best_params, monotone, device)
        progression.append(("2 + tuning", len(selected), g2, s2))
        print(f"      tuned -> {FR} gini={g2:0.4f}  stability={s2:0.4f}")

    if args.no_ensemble:
        print("\n[6/6] ENSEMBLE: skipped")
    else:
        print("\n[6/6] ENSEMBLE: rank-average blend of tuned LightGBM + XGBoost ...")
        try:
            fit_df, val_df, test_df = train_mat.loc[fit_ids], train_mat.loc[val_ids], serve_mat.loc[test_ids]
            # LightGBM leg (tuned if available)
            b_lgb, bit = lgbm_fit(fit_df, val_df, selected, params=best_params, monotone=monotone, device=device)
            p_lgb = lgbm_pred(b_lgb, fit_df, test_df, selected, bit)
            # XGBoost leg -- early-stop on VAL (never test), then predict test with best_iteration
            from step3lib.model_xgb import fit_eval_xgb
            import xgboost as xgb
            xg = fit_eval_xgb(fit_df, val_df, selected, target=TARGET,
                              monotone=(monotone or None))
            med, best_it = xg["medians"], xg["best_iteration"]
            Xte = test_df[selected].fillna(med).fillna(0.0).to_numpy(dtype=float)
            rng = {"iteration_range": (0, int(best_it) + 1)} if best_it is not None else {}
            p_xgb = xg["model"].predict(xgb.DMatrix(Xte, feature_names=list(selected)), **rng)
            g_xgb, s_xgb = score(test_df, p_xgb)
            print(f"      xgb  {FR} gini={g_xgb:0.4f}  stability={s_xgb:0.4f}")
            blend = rank_blend([p_lgb, p_xgb])
            g3, s3 = score(test_df, blend)
            progression.append(("3 + ensemble (lgb+xgb)", len(selected), g3, s3))
            print(f"      blend -> {FR} gini={g3:0.4f}  stability={s3:0.4f}")
        except ImportError as e:
            print(f"      ensemble skipped (missing wheel: {str(e)[:60]})")
        except Exception as e:
            print(f"      ensemble FAILED ({str(e)[:120]})")

    print("\n" + "=" * 80)
    print(f"PROGRESSION -- {FR} frame (higher = better)")
    print("=" * 80)
    head = f"{'stage':26s} {'#feats':>7s} {'test_gini':>10s} {'stability':>10s} {'d gini':>8s}"
    print(head)
    print("-" * len(head))
    base_g = progression[0][2]
    for name, nf, g, s in progression:
        print(f"{name:26s} {nf:7d} {g:10.4f} {s:10.4f} {g-base_g:+8.4f}")

    print("\nHOW TO READ THIS:")
    _frame_note = {
        "kg2ob": "Every row is KG->OB: trained on clean Kaggle-direct, served on the OB reconstruction "
                 "(a DIAGNOSTIC of the representation gap -- you would not have raw bureau features at serving).",
        "ob2ob": "Every row is OB->OB: trained AND served on the open-banking reconstruction "
                 "(this is the DEPLOYABLE pipeline -- reconstruct labelled training data into the serving representation).",
        "kg2kg": "Every row is KG->KG: trained AND served on clean Kaggle-direct (the CEILING; not deployable, "
                 "shows the best achievable if reconstruction were perfect).",
    }[args.frame]
    print(f"  * {_frame_note}")
    print("  * 'stability' is the Home Credit competition metric (mean weekly gini penalised for a")
    print("    falling trend + variance) -- it is what you should optimise, not raw gini alone.")
    print("  * Selection/tuning/ensemble are MODEL-SIDE gains (usually a few points).")
    if cross_rep:
        print("    The larger remaining lever for KG->OB is reconstruction FIDELITY: better renderers")
        print("    and more reconstructable source tables. The fidelity gate above flags the worst offenders.")
    else:
        print("    Because train and serve share one representation, there is no train/serve skew here.")
    print("  * Nothing here ever used the test fold to choose a feature, a round, or a hyperparameter.")
    print("\nDONE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
