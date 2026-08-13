#!/usr/bin/env python3
"""LGBM TEST RUN -- LightGBM on the open-banking reconstruction, built to scale
to the FULL 1.5M population without exhausting memory, with optional GPU and
built-in time-aware hyperparameter tuning.

WHAT IS ACTUALLY SLOW (read this):
  The model is NOT the bottleneck. LightGBM trains 1.5M x ~47 features in a few
  minutes on CPU. The slow part is REBUILDING the feature matrix from the raw
  Kaggle payment histories (~30s / 4k applicants -> a few hours at 1.5M). So
  this script:
    1. BUILDS THE MATRIX ONCE, streaming applicants in CHUNKS so the heavy
       per-payment objects never all live in RAM at the same time, and
    2. CACHES the numeric result to disk (lgbm_test_run/cache/).
  Every later train / tuning run reloads the cache in seconds. That caching +
  chunking is the real "don't wait ages" win. GPU is secondary (see README).

REQUIRES:  pip install lightgbm      (numpy / pandas / pyarrow you already have)
GPU is OPTIONAL and needs a GPU-ENABLED LightGBM build (the default pip wheel is
usually CPU-only). Pass --device gpu to try it; the script probes the GPU first
and FALLS BACK TO CPU cleanly if the wheel has no GPU support. See README.md.

EXAMPLES:
  # 1) fast smoke test: build + cache the 25k matrix, train on CPU
  python lgbm_test_run/run_lgbm.py "C:\\Users\\Josep\\Downloads\\homecredit" --max-cases 25000

  # 2) FULL run: build once (slow, cached), then train
  python lgbm_test_run/run_lgbm.py "...\\homecredit" --max-cases 0

  # 3) tune on the CACHED matrix (no rebuild): 30 time-aware random-search trials
  python lgbm_test_run/run_lgbm.py "...\\homecredit" --max-cases 0 --tune 30

  # 4) try the GPU (GTX 1660); falls back to CPU automatically if unavailable
  python lgbm_test_run/run_lgbm.py "...\\homecredit" --max-cases 0 --device gpu
"""
from __future__ import annotations
import argparse
import itertools
import json
import os
import pickle
import sys
import time

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# Bootstrap sys.path so this folder can live inside the step3 project and still
# import the shared engine (obcredit/, step3lib/, scripts/run_step3.py).
# Keep this folder INSIDE the project root (next to obcredit/ and step3lib/).
# --------------------------------------------------------------------------- #
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)                 # project root (has obcredit/, step3lib/)
_SCRIPTS = os.path.join(_ROOT, "scripts")
for _p in (_ROOT, _SCRIPTS, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from obcredit.adapters import KaggleAdapter, TrueLayerAdapter   # noqa: E402
from obcredit.feature_registry import REGISTRY                  # noqa: E402
from obcredit.pipeline import FeaturePipeline                   # noqa: E402
from obcredit.modeling.metrics import gini, gini_stability      # noqa: E402
from step3lib.kaggle_stream import canonical_pop_to_ground_truth  # noqa: E402
from step3lib.renderers import to_truelayer_payloads           # noqa: E402
from run_step3 import read_labels                              # noqa: E402

TARGET = "target"
WEEKCOL = "__week__"


def _build_stamp() -> str:
    try:
        with open(os.path.join(_ROOT, "VERSION.txt"), "r", encoding="utf-8") as vf:
            return vf.readline().strip()
    except OSError:
        return "BUILD ??? (VERSION.txt not found)"


def _chunks(iterable, size):
    """Yield lists of at most `size` items from any iterable/generator."""
    it = iter(iterable)
    while True:
        block = list(itertools.islice(it, size))
        if not block:
            return
        yield block


# --------------------------------------------------------------------------- #
# Feature matrix: build once (streamed + chunked), cache to disk, reuse forever.
# --------------------------------------------------------------------------- #
def _cache_paths(cache_dir: str, max_cases: int):
    tag = "full" if not max_cases else str(max_cases)
    return (os.path.join(cache_dir, f"ob_matrix_{tag}.pkl"),
            os.path.join(cache_dir, f"ob_matrix_{tag}.meta.json"))


def build_or_load_matrix(kaggle_dir: str, max_cases: int, batch: int,
                         cache_dir: str, build_stamp: str,
                         rebuild: bool = False) -> pd.DataFrame:
    os.makedirs(cache_dir, exist_ok=True)
    mat_path, meta_path = _cache_paths(cache_dir, max_cases)
    feats_now = list(REGISTRY.parity_names())

    if (not rebuild) and os.path.exists(mat_path) and os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as mf:
                meta = json.load(mf)
        except Exception:
            meta = {}
        fresh = (meta.get("build") == build_stamp
                 and meta.get("features") == feats_now
                 and meta.get("max_cases") == (max_cases or 0))
        if fresh:
            print(f"      [cache] reusing {os.path.basename(mat_path)} "
                  f"(built {meta.get('built_at', '?')}, {meta.get('rows', '?')} rows)")
            with open(mat_path, "rb") as f:
                return pickle.load(f)
        print("      [cache] stale (build / feature set / max-cases changed) -> rebuilding")

    mc = None if not max_cases else max_cases
    print("      reading labels (target + competition week) ...")
    labels = read_labels(kaggle_dir, max_cases=mc)
    print("      streaming Kaggle -> canonical -> open banking -> features (chunked) ...")
    adapter = KaggleAdapter.from_parquet_dir(kaggle_dir, max_cases=mc)
    pipe = FeaturePipeline()

    frames = []
    n_done = 0
    t0 = time.time()
    for chunk in _chunks(adapter.stream_canonical(), batch):
        gt = canonical_pop_to_ground_truth(chunk, labels=labels)
        canon = TrueLayerAdapter(to_truelayer_payloads(gt)).to_canonical()
        m = pipe.build_matrix(canon)
        idx = [str(i) for i in m.index]
        m[TARGET] = [labels.get(i, (None, None))[0] for i in idx]
        m[WEEKCOL] = [labels.get(i, (None, None))[1] for i in idx]
        frames.append(m)
        n_done += len(m)
        rate = n_done / max(1e-9, time.time() - t0)
        print(f"      built {n_done:,} rows  ({rate:0.0f}/s)", flush=True)
        del chunk, gt, canon, m            # free the heavy per-payment objects

    matrix = pd.concat(frames) if frames else pd.DataFrame()
    del frames

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
    return matrix


# --------------------------------------------------------------------------- #
# Split / impute
# --------------------------------------------------------------------------- #
def split_by_week(matrix: pd.DataFrame, cols):
    data = matrix.dropna(subset=[TARGET, WEEKCOL]).copy()
    if data.empty:
        raise SystemExit("ERROR: no labelled rows after join -- check the label source.")
    data = data.sort_values(WEEKCOL)
    cut = max(1, int(len(data) * 0.8))
    train, test = data.iloc[:cut], data.iloc[cut:]
    keep = [c for c in cols if c in data.columns]
    return train, test, keep


def _prep(train: pd.DataFrame, test: pd.DataFrame, cols):
    """Median-impute with TRAIN medians only (no leakage); NaN-only cols -> 0."""
    med = train[cols].median(numeric_only=True)
    tr = train[cols].fillna(med).fillna(0.0)
    te = test[cols].fillna(med).fillna(0.0)
    return tr, te, med


# --------------------------------------------------------------------------- #
# LightGBM params + fit
# --------------------------------------------------------------------------- #
def default_lgbm_params(seed: int = 42, device: str = "cpu", max_bin: int = 255) -> dict:
    p = {
        "objective": "binary",
        "metric": "auc",
        "boosting_type": "gbdt",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "max_depth": -1,
        "min_child_samples": 50,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 1,
        "lambda_l1": 0.0,
        "lambda_l2": 2.0,
        "max_bin": int(max_bin),
        "num_threads": 0,          # 0 = use all cores
        "verbose": -1,
        "seed": int(seed),
    }
    if device == "gpu":
        # OpenCL GPU backend (works on a GTX 1660). Smaller max_bin is much
        # faster on GPU and barely changes accuracy.
        p["device_type"] = "gpu"
        p["gpu_platform_id"] = 0
        p["gpu_device_id"] = 0
        p["max_bin"] = min(int(max_bin), 63)
    else:
        p["device_type"] = "cpu"
    return p


def fit_lgbm(train, test, cols, monotone=None, params=None, device="cpu",
             num_boost_round=3000, early_stopping_rounds=100, seed=42) -> dict:
    import lightgbm as lgb

    tr, te, med = _prep(train, test, cols)
    ytr = train[TARGET].astype(float).to_numpy()
    yte = test[TARGET].astype(float).to_numpy()

    p = dict(params) if params else default_lgbm_params(seed=seed, device=device)
    pos = float((ytr == 1).sum())
    neg = float((ytr == 0).sum())
    p["scale_pos_weight"] = (neg / pos) if pos > 0 else 1.0
    if monotone:
        p["monotone_constraints"] = [int(monotone.get(c, 0)) for c in cols]

    Xtr = tr.to_numpy(dtype=np.float32)
    Xte = te.to_numpy(dtype=np.float32)
    dtr = lgb.Dataset(Xtr, label=ytr, feature_name=list(cols), free_raw_data=False)
    dte = lgb.Dataset(Xte, label=yte, reference=dtr, free_raw_data=False)

    booster = lgb.train(
        p, dtr, num_boost_round=num_boost_round,
        valid_sets=[dtr, dte], valid_names=["train", "test"],
        callbacks=[lgb.early_stopping(early_stopping_rounds, verbose=False),
                   lgb.log_evaluation(0)],
    )
    best_it = booster.best_iteration or num_boost_round
    ptr = booster.predict(Xtr, num_iteration=best_it)
    pte = booster.predict(Xte, num_iteration=best_it)
    return {
        "model": booster, "medians": med, "cols": list(cols), "params": p,
        "best_iteration": int(best_it),
        "train_gini": float(gini(ytr, ptr)),
        "test_gini": float(gini(yte, pte)),
        "test_pred": pte,
    }


def _probe_gpu(seed: int = 42) -> bool:
    """Train a tiny model on the GPU to check the wheel actually supports it."""
    import lightgbm as lgb
    try:
        rng = np.random.RandomState(0)
        X = rng.rand(64, 5).astype(np.float32)
        y = (X[:, 0] > 0.5).astype(float)
        d = lgb.Dataset(X, label=y)
        lgb.train({"objective": "binary", "device_type": "gpu", "verbose": -1,
                   "max_bin": 63, "gpu_platform_id": 0, "gpu_device_id": 0},
                  d, num_boost_round=1)
        print("      [gpu] GPU backend OK")
        return True
    except Exception as e:
        msg = str(e).replace("\n", " ")[:140]
        print(f"      [gpu] not available ({msg}) -> using CPU")
        return False


# --------------------------------------------------------------------------- #
# Time-aware random-search tuning (scored by the competition stability metric)
# --------------------------------------------------------------------------- #
def tune_lgbm(train, test, cols, monotone, device, n_trials, seed, num_boost_round=1500):
    tr_sorted = train.sort_values(WEEKCOL)
    cut = max(1, int(len(tr_sorted) * 0.8))
    sub_tr, sub_val = tr_sorted.iloc[:cut], tr_sorted.iloc[cut:]
    if sub_val.empty:
        print("      [tune] not enough rows to carve a validation slice; skipping tuning")
        return None

    space = {
        "num_leaves": [15, 31, 63, 127, 255],
        "min_child_samples": [20, 50, 100, 200, 400],
        "learning_rate": [0.02, 0.03, 0.05, 0.08],
        "feature_fraction": [0.6, 0.7, 0.8, 0.9, 1.0],
        "bagging_fraction": [0.6, 0.7, 0.8, 0.9, 1.0],
        "lambda_l1": [0.0, 0.5, 1.0, 2.0],
        "lambda_l2": [0.0, 1.0, 2.0, 5.0, 10.0],
        "max_depth": [-1, 4, 6, 8, 12],
    }
    rng = np.random.RandomState(seed)
    best = None
    for t in range(n_trials):
        params = default_lgbm_params(seed=seed, device=device)
        for k, choices in space.items():
            params[k] = choices[int(rng.randint(len(choices)))]
        try:
            res = fit_lgbm(sub_tr, sub_val, cols, monotone=monotone, params=params,
                           device=device, num_boost_round=num_boost_round, seed=seed)
        except Exception as e:
            print(f"      trial {t + 1:>3}/{n_trials}: FAILED ({str(e)[:80]}); skipping")
            continue
        stab = gini_stability(sub_val[WEEKCOL].to_numpy(),
                              sub_val[TARGET].to_numpy(), res["test_pred"])
        score = stab["metric"] if stab["mean_gini"] > 0 else res["test_gini"]
        print(f"      trial {t + 1:>3}/{n_trials}: val_stability={score:0.4f} "
              f"val_Gini={res['test_gini']:0.4f}  "
              f"[leaves={params['num_leaves']}, mcs={params['min_child_samples']}, "
              f"lr={params['learning_rate']}, ff={params['feature_fraction']}, "
              f"l2={params['lambda_l2']}]")
        if best is None or score > best[0]:
            best = (float(score), dict(params))
    return best


# --------------------------------------------------------------------------- #
# Optional permutation importance (OFF by default -- expensive on the full set)
# --------------------------------------------------------------------------- #
def permutation_importance(res, test, n_repeats, seed=42):
    cols = res["cols"]
    med = res["medians"]
    booster = res["model"]
    best_it = res["best_iteration"]
    X = test[cols].fillna(med).fillna(0.0).to_numpy(dtype=np.float32)
    y = test[TARGET].astype(float).to_numpy()
    base = float(gini(y, booster.predict(X, num_iteration=best_it)))
    rs = np.random.RandomState(seed)
    out = []
    for j, c in enumerate(cols):
        drops = []
        for _ in range(n_repeats):
            m = X.copy()
            m[:, j] = rs.permutation(m[:, j])
            drops.append(base - float(gini(y, booster.predict(m, num_iteration=best_it))))
        out.append((c, float(np.mean(drops)), float(np.std(drops))))
    out.sort(key=lambda kv: kv[1], reverse=True)
    return base, out


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="LightGBM on the open-banking reconstruction (scalable + tunable).")
    ap.add_argument("kaggle_dir")
    ap.add_argument("--max-cases", type=int, default=25000, help="0 = full population")
    ap.add_argument("--batch", type=int, default=8000, help="applicants built per chunk (memory bound)")
    ap.add_argument("--device", choices=["cpu", "gpu"], default="cpu")
    ap.add_argument("--tune", type=int, default=0, help="N random-search trials (0 = off)")
    ap.add_argument("--no-monotone", action="store_true", help="disable monotone constraints (ablation)")
    ap.add_argument("--rebuild", action="store_true", help="ignore the feature cache and rebuild")
    ap.add_argument("--cache-dir", default=os.path.join(_HERE, "cache"))
    ap.add_argument("--perm-repeats", type=int, default=0, help="permutation importance repeats (0 = skip)")
    ap.add_argument("--max-bin", type=int, default=255)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    try:
        import lightgbm as lgb
    except ImportError:
        print("ERROR: lightgbm is not installed.\n       Install it with:  pip install lightgbm", file=sys.stderr)
        return 3
    if not os.path.isdir(args.kaggle_dir):
        print(f"ERROR: not a directory: {args.kaggle_dir}", file=sys.stderr)
        return 2

    stamp = _build_stamp()
    print("=" * 78)
    print("LGBM TEST RUN: LightGBM on the open-banking reconstruction")
    print(f">>> RUNNING: {stamp}")
    print(f">>> lightgbm {lgb.__version__} | device={args.device} | tune={args.tune} | "
          f"monotone={'off' if args.no_monotone else 'on'}")
    print("=" * 78)

    # Probe the GPU up-front so tuning + final fit use a known-good device.
    if args.device == "gpu" and not _probe_gpu(args.seed):
        args.device = "cpu"

    print("\n[1/4] building / loading cached feature matrix (streamed in chunks) ...")
    matrix = build_or_load_matrix(args.kaggle_dir, args.max_cases, args.batch,
                                  args.cache_dir, stamp, rebuild=args.rebuild)
    print(f"      matrix {matrix.shape}")

    feats = list(REGISTRY.parity_names())
    monotone = None if args.no_monotone else REGISTRY.monotone_map()

    print("[2/4] out-of-time split by week ...")
    train, test, cols = split_by_week(matrix, feats)
    print(f"      train rows={len(train):,}  test rows={len(test):,}  features={len(cols)}")

    params = None
    if args.tune > 0:
        print(f"[3/4] tuning: {args.tune} time-aware random-search trials (scored by stability) ...")
        best = tune_lgbm(train, test, cols, monotone, args.device, args.tune, args.seed)
        if best:
            params = best[1]
            print(f"      best validation score = {best[0]:0.4f}")
            print("      best params: " + ", ".join(
                f"{k}={params[k]}" for k in ("num_leaves", "min_child_samples", "learning_rate",
                                             "feature_fraction", "bagging_fraction",
                                             "lambda_l1", "lambda_l2", "max_depth")))
    else:
        print("[3/4] using default params (pass --tune N to search) ...")

    print("[4/4] training final LightGBM (out-of-time split) ...")
    res = fit_lgbm(train, test, cols, monotone=monotone, params=params,
                   device=args.device, seed=args.seed)
    print(f"      best_iteration = {res['best_iteration']}")
    print(f"      LightGBM train Gini = {res['train_gini']:0.4f}")
    print(f"      LightGBM  test Gini = {res['test_gini']:0.4f}")

    stab = gini_stability(test[WEEKCOL].to_numpy(), test[TARGET].to_numpy(), res["test_pred"])
    print(f"      test stability metric = {stab['metric']:0.4f}  "
          f"(mean weekly Gini={stab['mean_gini']:0.4f}, slope={stab['slope']:+0.5f}, "
          f"res_std={stab['res_std']:0.4f})")

    booster = res["model"]
    gains = sorted(zip(cols, booster.feature_importance(importance_type="gain")),
                   key=lambda kv: kv[1], reverse=True)
    print("\n      gain importance (top 20):")
    for name, g in gains[:20]:
        print(f"        {name:32s} {g:14.1f}")

    if args.perm_repeats > 0:
        print(f"\n      permutation importance on held-out test ({args.perm_repeats} repeats):")
        base, imp = permutation_importance(res, test, args.perm_repeats, seed=args.seed)
        print(f"        baseline test Gini = {base:0.4f}")
        for name, drop, std in imp:
            print(f"        {name:32s} {drop:+10.4f} {std:8.4f}")

    print("\nDONE. LightGBM trained on the open-banking reconstruction.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
