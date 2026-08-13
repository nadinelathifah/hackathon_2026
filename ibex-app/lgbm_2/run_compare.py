#!/usr/bin/env python3
"""LGBM 2 -- CROSS-REPRESENTATION comparison harness (FAST build).

Same logic as before -- the 2x2 of train-representation x test-representation on
the SAME applicants and SAME out-of-time split -- but rebuilt to be much faster
on the full 1.5M population.

                | Test: Kaggle-direct | Test: OB-reconstructed
  --------------|---------------------|-----------------------
  Train: Kaggle | KG->KG  (CEILING)   | KG->OB  (DEPLOYMENT) *
  Train: OB     |        --           | OB->OB  (FLOOR)

  * KG->OB is the headline: train on the cleanest real bureau signal, serve on
    the open-banking reconstruction. Its gap to the ceiling is the cost of
    reconstruction (train/serve skew).

--------------------------------------------------------------------------------
WHAT MADE IT SLOW BEFORE (and how this fixes it)
--------------------------------------------------------------------------------
1) REDUNDANT KG RE-RENDER (the big one).
   The previous version built the Kaggle-direct matrix by rendering ground truth
   BACK into Kaggle frames and re-parsing them:
       build_matrix(KaggleAdapter(to_kaggle_frames(gt)).to_canonical())
   That doubled the per-applicant work on top of the OB render -> ~70/s.
   But KaggleAdapter.stream_canonical() ALREADY yields canonical applicants, and
   FeaturePipeline.build_matrix() consumes canonical directly. So the true
   Kaggle-direct matrix is just:
       build_matrix(chunk)                      # <-- essentially free
   This is also *cleaner*: it's the real Kaggle data, not a re-rendered copy --
   exactly the "train on the cleanest possible signal" you asked for.

2) SINGLE-PROCESS.
   The reconstruction is per-applicant Python work, so it scales across CPU
   cores. This version fans the build out over a process pool (default: all
   cores but one). Near-linear speed-up.

3) REBUILDING OB YOU ALREADY HAVE.
   Point --reuse-ob at the full OB matrix you built with lgbm_test_run and the
   OB side is loaded instantly; only the (now-cheap) KG matrix is built.

--------------------------------------------------------------------------------
WHY NOT GPU / RAPIDS FOR THE BUILD
--------------------------------------------------------------------------------
The build bottleneck is per-applicant Python object reconstruction, NOT
vectorised DataFrame math, so a GPU / cuDF cannot accelerate it (cuDF is not a
drop-in here, and it's a heavy, fragile CUDA install). The right lever is
multi-core + not doing redundant work -- both applied above. LightGBM TRAINING
can still use your GPU via --device gpu, but that step is only seconds; it is not
where the time goes.

Requires numpy + pandas always; lightgbm and/or xgboost only for those models.
Must live INSIDE the step3 project root (next to obcredit/, step3lib/, scripts/).

EXAMPLES
  # FULL run, all cores, all three models
  python lgbm_2/run_compare.py "C:\\Users\\Josep\\Downloads\\homecredit" --max-cases 0

  # FULL run but reuse the OB matrix from lgbm_test_run (only builds KG -> fastest)
  python lgbm_2/run_compare.py "...\\homecredit" --max-cases 0 \
        --reuse-ob ..\\lgbm_test_run\\cache\\ob_matrix_full.pkl

  # cap workers / pick models / GPU training for LightGBM
  python lgbm_2/run_compare.py "...\\homecredit" --max-cases 0 --workers 6 --models lgbm --device gpu
"""
from __future__ import annotations
import argparse
import itertools
import json
import os
import pickle
import sys
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Bootstrap sys.path so this folder can live inside the step3 project and still
# import the shared engine (obcredit/, step3lib/, scripts/run_step3.py).
# --------------------------------------------------------------------------- #
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)                 # project root (has obcredit/, step3lib/)
_SCRIPTS = os.path.join(_ROOT, "scripts")
for _p in (_ROOT, _SCRIPTS, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from obcredit.adapters import KaggleAdapter, TrueLayerAdapter        # noqa: E402
from obcredit.feature_registry import REGISTRY                       # noqa: E402
from obcredit.pipeline import FeaturePipeline                        # noqa: E402
from obcredit.modeling.metrics import gini, gini_stability           # noqa: E402
from step3lib.kaggle_stream import canonical_pop_to_ground_truth     # noqa: E402
from step3lib.renderers import to_kaggle_frames, to_truelayer_payloads  # noqa: E402
from step3lib.model import fit_eval as fit_eval_logreg               # noqa: E402
from run_step3 import read_labels                                    # noqa: E402

TARGET = "target"
WEEKCOL = "__week__"


def _build_stamp() -> str:
    try:
        with open(os.path.join(_ROOT, "VERSION.txt"), "r", encoding="utf-8") as vf:
            return vf.readline().strip()
    except OSError:
        return "BUILD ??? (VERSION.txt not found)"


def _chunks(iterable, size):
    it = iter(iterable)
    while True:
        block = list(itertools.islice(it, size))
        if not block:
            return
        yield block


# --------------------------------------------------------------------------- #
# Parallel worker: reconstruct ONE chunk of canonical applicants into feature
# matrices. Top-level (picklable) so it works with spawn (Windows) and fork.
#   KG (raw)      = build_matrix(chunk)                         <- free, real Kaggle
#   KG (rendered) = build_matrix(kaggle-render of ground truth) <- strict parity basis
#   OB            = build_matrix(truelayer-render of ground truth)
# Labels/target are attached later in the parent (features don't depend on them).
# --------------------------------------------------------------------------- #
_PIPE = None
_FLAGS = None  # (need_kg, need_ob, kg_mode)


def _init_worker(need_kg, need_ob, kg_mode):
    global _PIPE, _FLAGS
    _PIPE = FeaturePipeline()
    _FLAGS = (need_kg, need_ob, kg_mode)


def _work_chunk(chunk):
    need_kg, need_ob, kg_mode = _FLAGS
    res = {}
    gt = None
    if need_ob or (need_kg and kg_mode == "rendered"):
        gt = canonical_pop_to_ground_truth(chunk)
    if need_kg:
        if kg_mode == "rendered":
            res["kg"] = _PIPE.build_matrix(KaggleAdapter(to_kaggle_frames(gt)).to_canonical())
        else:
            res["kg"] = _PIPE.build_matrix(chunk)
    if need_ob:
        res["ob"] = _PIPE.build_matrix(TrueLayerAdapter(to_truelayer_payloads(gt)).to_canonical())
    return res


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
def _cache_paths(cache_dir, kind, max_cases, kg_mode=None):
    tag = "full" if not max_cases else str(max_cases)
    suffix = f"_{kg_mode}" if (kind == "kg" and kg_mode) else ""
    return (os.path.join(cache_dir, f"{kind}_matrix_{tag}{suffix}.pkl"),
            os.path.join(cache_dir, f"{kind}_matrix_{tag}{suffix}.meta.json"))


def _load_cached(mat_path, meta_path, build_stamp, feats_now, max_cases):
    if not (os.path.exists(mat_path) and os.path.exists(meta_path)):
        return None
    try:
        with open(meta_path, "r", encoding="utf-8") as mf:
            meta = json.load(mf)
    except Exception:
        return None
    if (meta.get("build") == build_stamp and meta.get("features") == feats_now
            and meta.get("max_cases") == (max_cases or 0)):
        with open(mat_path, "rb") as f:
            return pickle.load(f)
    return None


def _write_cache(mat_path, meta_path, matrix, build_stamp, feats_now, max_cases):
    try:
        with open(mat_path, "wb") as f:
            pickle.dump(matrix, f, protocol=4)
        with open(meta_path, "w", encoding="utf-8") as mf:
            json.dump({"build": build_stamp, "features": feats_now,
                       "max_cases": (max_cases or 0), "rows": int(len(matrix)),
                       "built_at": time.strftime("%Y-%m-%d %H:%M:%S")}, mf)
        print(f"      [cache] wrote {os.path.basename(mat_path)} ({len(matrix):,} rows)")
    except Exception as e:
        print(f"      [cache] WARNING: could not write cache ({e}); continuing in-memory")


def _attach_labels(m, labels):
    m.index = [str(i) for i in m.index]
    ids = list(m.index)
    m[TARGET] = [labels.get(i, (None, None))[0] for i in ids]
    m[WEEKCOL] = [labels.get(i, (None, None))[1] for i in ids]
    return m


# --------------------------------------------------------------------------- #
# Build both matrices in ONE streamed pass, fanned out over a process pool.
# --------------------------------------------------------------------------- #
def build_or_load_matrices(kaggle_dir, max_cases, batch, cache_dir, build_stamp,
                           workers, kg_mode, rebuild=False, reuse_ob=None):
    os.makedirs(cache_dir, exist_ok=True)
    feats_now = list(REGISTRY.parity_names())
    kg_path, kg_meta = _cache_paths(cache_dir, "kg", max_cases, kg_mode)
    ob_path, ob_meta = _cache_paths(cache_dir, "ob", max_cases)

    kg = None if rebuild else _load_cached(kg_path, kg_meta, build_stamp, feats_now, max_cases)

    ob = None
    if reuse_ob:
        if os.path.exists(reuse_ob):
            print(f"      [cache] reusing external OB matrix: {reuse_ob}")
            with open(reuse_ob, "rb") as f:
                ob = pickle.load(f)
            ob.index = [str(i) for i in ob.index]
        else:
            print(f"      [cache] --reuse-ob path not found ({reuse_ob}); will build OB")
    if ob is None and not rebuild:
        ob = _load_cached(ob_path, ob_meta, build_stamp, feats_now, max_cases)

    if kg is not None and ob is not None:
        print(f"      [cache] reusing KG ({len(kg):,} rows) + OB ({len(ob):,} rows)")
        return kg, ob

    need_kg, need_ob = kg is None, ob is None
    mc = None if not max_cases else max_cases
    print("      reading labels (target + competition week) ...")
    labels = read_labels(kaggle_dir, max_cases=mc)
    what = " + ".join([x for x, n in (("KG", need_kg), ("OB", need_ob)) if n])
    print(f"      building {what}  |  workers={workers}  |  kg_mode={kg_mode}  |  batch={batch}")

    adapter = KaggleAdapter.from_parquet_dir(kaggle_dir, max_cases=mc)
    tasks = _chunks(adapter.stream_canonical(), batch)

    kg_frames, ob_frames = [], []
    n_done = 0
    t0 = time.time()

    def _consume(res_iter):
        nonlocal n_done
        for res in res_iter:
            if "kg" in res:
                kg_frames.append(res["kg"])
                n_done += len(res["kg"])
            elif "ob" in res:
                n_done += len(res["ob"])
            if "ob" in res:
                ob_frames.append(res["ob"])
            rate = n_done / max(1e-9, time.time() - t0)
            eta = ("?" if not max_cases else f"{max(0,(max_cases-n_done))/max(1e-9,rate):0.0f}s")
            print(f"      built {n_done:,} applicants  ({rate:0.0f}/s, eta {eta})", flush=True)

    if workers <= 1:
        _init_worker(need_kg, need_ob, kg_mode)
        _consume(map(_work_chunk, tasks))
    else:
        with Pool(processes=workers, initializer=_init_worker,
                  initargs=(need_kg, need_ob, kg_mode)) as pool:
            _consume(pool.imap_unordered(_work_chunk, tasks))

    if need_kg:
        kg = pd.concat(kg_frames) if kg_frames else pd.DataFrame()
        kg = _attach_labels(kg, labels)
        _write_cache(kg_path, kg_meta, kg, build_stamp, feats_now, max_cases)
    if need_ob:
        ob = pd.concat(ob_frames) if ob_frames else pd.DataFrame()
        ob = _attach_labels(ob, labels)
        _write_cache(ob_path, ob_meta, ob, build_stamp, feats_now, max_cases)
    return kg, ob


# --------------------------------------------------------------------------- #
# ONE shared out-of-time split (by week) on case_ids present in BOTH matrices.
# --------------------------------------------------------------------------- #
def shared_split(kg, ob):
    common = kg.index.intersection(ob.index)
    sub = kg.loc[common, [TARGET, WEEKCOL]].dropna(subset=[TARGET, WEEKCOL])
    if sub.empty:
        raise SystemExit("ERROR: no labelled rows shared by KG and OB -- check the label source.")
    order = sub.sort_values(WEEKCOL).index
    cut = max(1, int(len(order) * 0.8))
    return list(order[:cut]), list(order[cut:])


# --------------------------------------------------------------------------- #
# Train/serve drift: which features shift most from clean Kaggle -> OB serving.
# (KG is raw Kaggle-direct, OB is the reconstruction, so this is genuine
#  deployment drift -- exactly what drives cost(rec).)
# --------------------------------------------------------------------------- #
def drift_report(kg, ob, cols, top=15):
    common = kg.index.intersection(ob.index)
    a = kg.loc[common, cols].apply(pd.to_numeric, errors="coerce")
    b = ob.loc[common, cols].apply(pd.to_numeric, errors="coerce")
    rows = []
    for c in cols:
        x, y = a[c], b[c]
        sd = x.std(ddof=0)
        std_shift = (abs(x.mean() - y.mean()) / sd) if (sd and sd > 0) else np.nan
        rows.append((c, x.mean(), y.mean(), std_shift, x.corr(y)))
    df = pd.DataFrame(rows, columns=["feature", "mean_KG", "mean_OB", "std_shift", "corr"])
    return df.sort_values("std_shift", ascending=False, na_position="last").head(top)


# --------------------------------------------------------------------------- #
# LightGBM (inlined so this folder is self-contained)
# --------------------------------------------------------------------------- #
def _prep_lgbm(train, test, cols):
    med = train[cols].median(numeric_only=True)
    return train[cols].fillna(med).fillna(0.0), test[cols].fillna(med).fillna(0.0)


def default_lgbm_params(seed=42, device="cpu", max_bin=255):
    p = {"objective": "binary", "metric": "auc", "boosting_type": "gbdt",
         "learning_rate": 0.05, "num_leaves": 31, "max_depth": -1,
         "min_child_samples": 50, "feature_fraction": 0.8, "bagging_fraction": 0.8,
         "bagging_freq": 1, "lambda_l1": 0.0, "lambda_l2": 2.0,
         "max_bin": int(max_bin), "num_threads": 0, "verbose": -1, "seed": int(seed)}
    if device == "gpu":
        p.update({"device_type": "gpu", "gpu_platform_id": 0, "gpu_device_id": 0,
                  "max_bin": min(int(max_bin), 63)})
    else:
        p["device_type"] = "cpu"
    return p


def fit_eval_lgbm(train, test, cols, monotone=None, device="cpu",
                  num_boost_round=3000, early_stopping_rounds=100, seed=42):
    import lightgbm as lgb
    tr, te = _prep_lgbm(train, test, cols)
    ytr = train[TARGET].astype(float).to_numpy()
    yte = test[TARGET].astype(float).to_numpy()
    p = default_lgbm_params(seed=seed, device=device)
    pos, neg = float((ytr == 1).sum()), float((ytr == 0).sum())
    p["scale_pos_weight"] = (neg / pos) if pos > 0 else 1.0
    if monotone:
        p["monotone_constraints"] = [int(monotone.get(c, 0)) for c in cols]
    Xtr = tr.to_numpy(dtype=np.float32)
    Xte = te.to_numpy(dtype=np.float32)
    dtr = lgb.Dataset(Xtr, label=ytr, feature_name=list(cols), free_raw_data=False)
    dte = lgb.Dataset(Xte, label=yte, reference=dtr, free_raw_data=False)
    booster = lgb.train(p, dtr, num_boost_round=num_boost_round,
                        valid_sets=[dtr, dte], valid_names=["train", "test"],
                        callbacks=[lgb.early_stopping(early_stopping_rounds, verbose=False),
                                   lgb.log_evaluation(0)])
    best_it = booster.best_iteration or num_boost_round
    ptr = booster.predict(Xtr, num_iteration=best_it)
    pte = booster.predict(Xte, num_iteration=best_it)
    return {"best_iteration": int(best_it), "train_gini": float(gini(ytr, ptr)),
            "test_gini": float(gini(yte, pte)), "test_pred": pte}


def _probe_gpu():
    import lightgbm as lgb
    try:
        rng = np.random.RandomState(0)
        X = rng.rand(64, 5).astype(np.float32)
        y = (X[:, 0] > 0.5).astype(float)
        lgb.train({"objective": "binary", "device_type": "gpu", "verbose": -1,
                   "max_bin": 63, "gpu_platform_id": 0, "gpu_device_id": 0},
                  lgb.Dataset(X, label=y), num_boost_round=1)
        print("      [gpu] LightGBM GPU backend OK")
        return True
    except Exception as e:
        print(f"      [gpu] not available ({str(e).replace(chr(10), ' ')[:100]}) -> CPU")
        return False


# --------------------------------------------------------------------------- #
# Generic cell: train on one representation, test on another (shared ids).
# --------------------------------------------------------------------------- #
def eval_cell(model, train_mat, test_mat, train_ids, test_ids, cols,
              monotone=None, device="cpu"):
    train, test = train_mat.loc[train_ids], test_mat.loc[test_ids]
    if model == "logreg":
        res = fit_eval_logreg(train, test, cols, target=TARGET)
    elif model == "xgb":
        from step3lib.model_xgb import fit_eval_xgb
        res = fit_eval_xgb(train, test, cols, target=TARGET, monotone=monotone)
    elif model == "lgbm":
        res = fit_eval_lgbm(train, test, cols, monotone=monotone, device=device)
    else:
        raise ValueError(model)
    stab = gini_stability(test[WEEKCOL].to_numpy(dtype=float),
                          test[TARGET].to_numpy(dtype=float), res["test_pred"])
    return {"train_gini": res["train_gini"], "test_gini": res["test_gini"],
            "stability": stab.get("metric"), "mean_weekly": stab.get("mean_gini"),
            "slope": stab.get("slope")}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Cross-representation (KG/OB) model comparison -- fast build.")
    ap.add_argument("kaggle_dir")
    ap.add_argument("--max-cases", type=int, default=25000, help="0 = full population")
    ap.add_argument("--batch", type=int, default=3000, help="applicants per task (smaller = better load balancing)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1),
                    help="build processes (1 = serial)")
    ap.add_argument("--kg-mode", choices=["raw", "rendered"], default="raw",
                    help="raw = real Kaggle-direct (fast, default); rendered = GT->kaggle re-render (strict parity basis, slow)")
    ap.add_argument("--models", default="logreg,xgb,lgbm", help="comma list from {logreg,xgb,lgbm}")
    ap.add_argument("--device", choices=["cpu", "gpu"], default="cpu", help="LightGBM training device")
    ap.add_argument("--no-monotone", action="store_true", help="disable monotone constraints")
    ap.add_argument("--rebuild", action="store_true", help="ignore caches and rebuild")
    ap.add_argument("--reuse-ob", default=None, help="path to an existing ob_matrix_*.pkl to reuse (skips OB build)")
    ap.add_argument("--cache-dir", default=os.path.join(_HERE, "cache"))
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    if not os.path.isdir(args.kaggle_dir):
        print(f"ERROR: not a directory: {args.kaggle_dir}", file=sys.stderr)
        return 2

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    device = args.device
    workers = max(1, args.workers)
    stamp = _build_stamp()

    print("=" * 80)
    print("LGBM 2: CROSS-REPRESENTATION comparison (KG->KG ceiling / OB->OB floor / KG->OB deploy)")
    print(f">>> RUNNING: {stamp}")
    print(f">>> models={models} | monotone={'off' if args.no_monotone else 'on'} | "
          f"lgbm_device={device} | workers={workers} | kg_mode={args.kg_mode}")
    print("=" * 80)

    if device == "gpu" and "lgbm" in models and not _probe_gpu():
        device = "cpu"

    print("\n[1/4] building / loading KG + OB feature matrices ...")
    kg, ob = build_or_load_matrices(args.kaggle_dir, args.max_cases, args.batch,
                                    args.cache_dir, stamp, workers, args.kg_mode,
                                    rebuild=args.rebuild, reuse_ob=args.reuse_ob)
    print(f"      KG matrix {kg.shape}   OB matrix {ob.shape}")

    cols = [c for c in REGISTRY.parity_names() if c in kg.columns and c in ob.columns]
    monotone = None if args.no_monotone else REGISTRY.monotone_map()
    print(f"      {len(cols)} shared parity features")

    print("\n[2/4] train(KG) -> serve(OB) feature drift (top movers) ...")
    dr = drift_report(kg, ob, cols)
    with pd.option_context("display.width", 120, "display.max_rows", None):
        print(dr.to_string(index=False, float_format=lambda v: f"{v:0.4f}"))

    print("\n[3/4] shared out-of-time split (by week) ...")
    train_ids, test_ids = shared_split(kg, ob)
    print(f"      train applicants={len(train_ids):,}  test applicants={len(test_ids):,}")

    print("\n[4/4] training the 2x2 for each model ...")
    results = {}
    for model in models:
        try:
            cells = {}
            for name, tr_mat, te_mat in (("KG->KG", kg, kg), ("OB->OB", ob, ob), ("KG->OB", kg, ob)):
                c = eval_cell(model, tr_mat, te_mat, train_ids, test_ids, cols,
                              monotone=monotone, device=device)
                cells[name] = c
                print(f"      {model:6s} {name}: train={c['train_gini']:0.4f} "
                      f"test={c['test_gini']:0.4f}  stability={c['stability']:0.4f} "
                      f"(mean weekly={c['mean_weekly']:0.4f}, slope={c['slope']:+0.5f})")
            results[model] = cells
        except ImportError as e:
            print(f"      {model}: SKIPPED (missing wheel: {str(e)[:60]})")
        except Exception as e:
            print(f"      {model}: FAILED ({str(e)[:120]})")

    print("\n" + "=" * 80)
    print("SUMMARY -- test Gini (higher = better).  retained = KG->OB / KG->KG.")
    print("=" * 80)
    header = (f"{'model':8s} {'KG->KG':>9s} {'OB->OB':>9s} {'KG->OB':>9s} "
              f"{'cost(rec)':>10s} {'retained':>9s} {'KG->OB stab':>12s}")
    print(header)
    print("-" * len(header))
    for model, cells in results.items():
        kk, oo, ko = cells["KG->KG"]["test_gini"], cells["OB->OB"]["test_gini"], cells["KG->OB"]["test_gini"]
        ret = (ko / kk * 100.0) if kk > 0 else float("nan")
        print(f"{model:8s} {kk:9.4f} {oo:9.4f} {ko:9.4f} {kk-ko:10.4f} {ret:8.1f}% "
              f"{cells['KG->OB']['stability']:12.4f}")

    print("\nHOW TO READ THIS:")
    print("  * KG->KG  = ceiling: most signal the features can hold (real Kaggle data).")
    print("  * OB->OB  = floor: train AND serve on the reconstruction (no skew, capped signal).")
    print("  * KG->OB  = DEPLOYMENT: train on clean history, serve on open banking. HEADLINE.")
    print("  * cost(rec) = KG->KG - KG->OB = price of reconstruction (train/serve skew).")
    print("  * If KG->OB >= OB->OB, training on clean data wins despite the skew -- use it.")
    print("  * The drift table above tells you WHICH features move most from KG train -> OB serve.")
    print("\nDONE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
