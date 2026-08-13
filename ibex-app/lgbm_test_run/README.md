# LGBM TEST RUN

LightGBM on the open-banking reconstruction, built to scale to the **full 1.5M**
population without running out of memory, with **optional GPU** and **built-in
time-aware hyperparameter tuning**.

Drop this folder inside your `step3` project (next to `obcredit/` and
`step3lib/`). It self-bootstraps the import path, so run it from the project
root:

```bash
python lgbm_test_run/run_lgbm.py "<path-to-homecredit>" [options]
```

---

## The important idea: build once, train/tune many times

**The model is not what's slow.** LightGBM trains 1.5M x ~47 features in a few
minutes. The slow part is rebuilding the feature matrix from the raw Kaggle
payment histories (~30s per 4k applicants -> a few hours at 1.5M).

So this script **builds the matrix once, streaming applicants in chunks** (the
heavy per-payment objects are freed after each chunk, so RAM stays bounded), and
**caches the numeric result** to `lgbm_test_run/cache/`. Every later run --
including every tuning trial -- reloads the cache in seconds.

- First full build: slow (hours), one time.
- Everything after: seconds to load + minutes to train.
- The cache auto-invalidates if the BUILD version, the feature set, or
  `--max-cases` changes. Force a rebuild with `--rebuild`.

### Recommended workflow

```bash
# 1) sanity check on 25k (fast)
python lgbm_test_run/run_lgbm.py "...\homecredit" --max-cases 25000

# 2) build + cache the FULL matrix once (slow), train with defaults
python lgbm_test_run/run_lgbm.py "...\homecredit" --max-cases 0

# 3) tune on the cached full matrix (NO rebuild -- reuses the cache)
python lgbm_test_run/run_lgbm.py "...\homecredit" --max-cases 0 --tune 40
```

---

## Memory control

- `--batch N` (default 8000): how many applicants are turned into features per
  chunk. Lower it (e.g. `--batch 4000`) if RAM is tight during the build; raise
  it for a bit more speed if you have headroom.
- The final numeric matrix for the full population is roughly 1.5M x ~50 as
  float32 (~300 MB) -- comfortable on a 16 GB machine. Training makes one more
  compact binned copy inside LightGBM.
- If you ever hit a memory wall at the very end, train in stages with a smaller
  `--max-cases` first; the cache for each size is kept separately.

---

## GPU (GTX 1660 / WSL) -- optional, and honestly secondary

```bash
python lgbm_test_run/run_lgbm.py "...\homecredit" --max-cases 0 --device gpu
```

The script **probes the GPU first** and **falls back to CPU automatically** if
the installed LightGBM has no GPU support -- so `--device gpu` is always safe to
try.

**Two honest caveats:**

1. **The default `pip install lightgbm` wheel is usually CPU-only.** GPU needs a
   GPU-enabled build. On WSL (Ubuntu) the reliable route is to build with the
   OpenCL backend:
   ```bash
   sudo apt-get install -y build-essential cmake libboost-dev libboost-system-dev \
       libboost-filesystem-dev ocl-icd-opencl-dev opencl-headers
   pip install lightgbm --config-settings=cmake.define.USE_GPU=ON
   ```
   (You also need the NVIDIA OpenCL runtime, which comes with the driver.)
2. **GPU may not actually be faster here.** LightGBM's GPU backend accelerates
   histogram construction, which mostly pays off with *hundreds* of features. At
   ~47 features the CPU build is already fast, and the real time sink is the
   feature build (which the GPU does not touch). Use GPU if you like, but the
   caching above is the change that removes the "waiting ages" problem.

When GPU is active the script lowers `max_bin` to 63 (much faster on GPU, minimal
accuracy cost).

---

## Tuning

`--tune N` runs `N` random-search trials over `num_leaves`,
`min_child_samples`, `learning_rate`, `feature_fraction`, `bagging_fraction`,
`lambda_l1/l2`, and `max_depth`.

- Each trial trains on an **earlier** slice of the training weeks and is scored
  on a **later** validation slice -- an out-of-time split -- using the **Home
  Credit stability metric** (`mean weekly Gini + 88*min(0,slope) - 0.5*std`),
  not raw Gini. That matches how the competition actually judges a model and
  rewards models whose weekly performance does not decay.
- The best configuration is then retrained on the full training set and
  evaluated on the held-out test weeks.
- No scikit-learn / Optuna needed -- pure NumPy random search.

---

## All options

| flag | default | meaning |
|---|---|---|
| `--max-cases N` | 25000 | applicants to use; **0 = full population** |
| `--batch N` | 8000 | applicants per build chunk (memory bound) |
| `--device cpu\|gpu` | cpu | GPU probed first, falls back to CPU |
| `--tune N` | 0 | N time-aware random-search trials (0 = off) |
| `--no-monotone` | off | disable monotone constraints (ablation) |
| `--rebuild` | off | ignore the feature cache and rebuild it |
| `--cache-dir PATH` | `lgbm_test_run/cache` | where cached matrices live |
| `--perm-repeats N` | 0 | permutation importance repeats (0 = skip; slow on full data) |
| `--max-bin N` | 255 | histogram bins (GPU auto-caps at 63) |
| `--seed N` | 42 | RNG seed |

---

## What it prints

- out-of-time (split-by-week) **train / test Gini**
- the **stability metric** on the test weeks (mean weekly Gini, slope, residual std)
- **gain importance** (top 20)
- optional **permutation importance** (`--perm-repeats > 0`)

The model is trained on the **open-banking reconstruction** (what inference would
actually see), so there is no train/serve skew -- identical to `run_xgb.py`,
using the same shared feature engine, split, and metrics.

---

## Requirements

```bash
pip install lightgbm     # numpy / pandas / pyarrow you already have
```
