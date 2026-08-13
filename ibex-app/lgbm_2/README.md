# LGBM 2 — cross-representation comparison (fast build)

Same logic as before — the **2×2 of train-representation × test-representation** on
the *same* applicants and *same* out-of-time split — rebuilt to run **much faster**
on the full 1.5M population.

|                | Test: Kaggle-direct | Test: OB-reconstructed |
|----------------|---------------------|------------------------|
| **Train: Kaggle-direct** | **KG→KG** — ceiling | ⭐ **KG→OB** — deployment |
| **Train: OB-reconstructed** | — | **OB→OB** — floor |

- **KG→KG (ceiling):** most signal the features can hold (real Kaggle data).
- **OB→OB (floor):** train *and* serve on the reconstruction — what `lgbm_test_run` reported (0.3364).
- **⭐ KG→OB (deployment, headline):** train on the cleanest real bureau signal,
  serve on the open-banking reconstruction. Gap to the ceiling = **cost of reconstruction**.

## Why it's now fast (what was slow before)

1. **No redundant Kaggle re-render (the big one).** The first version built the
   KG matrix by rendering ground truth *back* into Kaggle frames and re-parsing
   them — doubling the per-applicant work on top of the OB render (→ ~70/s).
   But `stream_canonical()` already yields canonical applicants and
   `build_matrix()` consumes them directly, so the true Kaggle-direct matrix is
   just `build_matrix(chunk)` — essentially free, and *cleaner* (it's the real
   Kaggle data, not a re-rendered copy). This is the default (`--kg-mode raw`).
2. **Multi-core build.** The reconstruction is per-applicant Python work, so it
   fans out across a process pool (default: all cores but one). Near-linear.
3. **Reuse the OB matrix you already built.** `--reuse-ob <path>` loads the full
   OB matrix from `lgbm_test_run` instantly; only the (now-cheap) KG side builds.

Both matrices are cached, so re-runs reload in seconds.

## Why not GPU / RAPIDS for the build
The build bottleneck is per-applicant Python object reconstruction, **not**
vectorised DataFrame math — a GPU / cuDF can't accelerate it (cuDF isn't a
drop-in here, and it's a heavy CUDA install). The real levers are multi-core +
not doing redundant work, both applied. LightGBM **training** can still use your
GPU via `--device gpu`, but that step is only seconds — it isn't where time goes.

## Location / requirements
Keep this folder **inside the step3 project root** (next to `obcredit/`,
`step3lib/`, `scripts/`). Needs `numpy` + `pandas` always; `lightgbm` / `xgboost`
only for those models (logreg is pure NumPy).

## Usage
```bash
# FULL run, all cores, all three models
python lgbm_2/run_compare.py "C:\Users\Josep\Downloads\homecredit" --max-cases 0

# FULL run but reuse the OB matrix from lgbm_test_run (only builds KG -> fastest)
python lgbm_2/run_compare.py "...\homecredit" --max-cases 0 \
      --reuse-ob ..\lgbm_test_run\cache\ob_matrix_full.pkl

# cap workers / pick models / GPU training for LightGBM
python lgbm_2/run_compare.py "...\homecredit" --max-cases 0 --workers 6 --models lgbm --device gpu
```

### Key flags
| flag | meaning |
|------|---------|
| `--max-cases N` | sample first N applicants (`0` = full 1.5M) |
| `--workers N` | build processes (default all cores − 1; `1` = serial) |
| `--kg-mode raw\|rendered` | `raw` (default, fast, real Kaggle-direct) or `rendered` (GT→kaggle re-render, strict parity basis, slow) |
| `--reuse-ob PATH` | load an existing `ob_matrix_*.pkl` instead of rebuilding OB |
| `--models` | any of `logreg,xgb,lgbm` |
| `--device gpu` | GPU for LightGBM **training** (auto-falls back to CPU) |
| `--no-monotone` | disable monotone constraints (ablation) |
| `--rebuild` | ignore caches and rebuild |
| `--batch N` | applicants per task (smaller = better load balancing) |

## What it prints
1. **Train→serve drift** — the features whose distribution moves most from clean
   Kaggle training to OB serving (the drivers of `cost(rec)`).
2. **Per-model 2×2** — train/test Gini + stability per cell.
3. **Summary** — `KG→KG`, `OB→OB`, `KG→OB`, `cost(rec) = KG→KG − KG→OB`,
   `retained % = KG→OB / KG→KG`, and KG→OB stability.

**Reading it:** if `KG→OB ≥ OB→OB`, training on clean data wins despite the skew
— adopt KG→OB. `cost(rec)` is the "price of reconstruction"; the drift table
attributes it feature-by-feature.

> Note on parity: because `--kg-mode raw` trains on real Kaggle data and serves
> the GT-based OB reconstruction, `cost(rec)` reflects the **total** train/serve
> skew (idealisation + open-banking channel). To isolate the open-banking channel
> only (same ground-truth basis on both sides), use the parity suite in
> `run_step3.py` (mean match rate ≈ 0.93) or run this with `--kg-mode rendered`.

> `run_compare.py` is the pre-selection **baseline**. The Gini-pushing steps
> (selection → tuning → ensemble) live in `run_boost.py` below.

---

# `run_boost.py` — push KG→OB Gini higher

Runs **on the cached matrices** (`run_compare.py` builds them once; `run_boost`
reuses them — no rebuild). Everything is scored in the **KG→OB deployment frame**
(train on clean Kaggle-direct, serve on the OB reconstruction), because that's the
number the dissertation defends. It reports a **progression table** so you see each
step's contribution:

| stage | what it does |
|-------|--------------|
| **0 baseline** | LightGBM, all shared parity features, default params |
| **1 + selection** | 4-gate funnel prunes noisy / unreliable / redundant features |
| **2 + tuning** | random search over LightGBM params, **scored by stability** |
| **3 + ensemble** | rank-average blend of the tuned LightGBM + XGBoost |

### No-leakage protocol (the defensible bit)
- `shared_split` → **train / test** by week (out-of-time). **Test is touched only
  to score the final number of each stage.**
- train is further split by week into **fit / val**. **All** selection and tuning
  decisions use fit→val only — the test fold never informs a feature choice, an
  early-stopping round, or a hyperparameter.

### Selection funnel (each gate logged with the count it removes)
1. **drift gate** — drop features whose train(KG)→serve(OB) distribution shift is
   too large (`std_shift > --drift-max`); they reconstruct badly and hurt serving.
2. **univariate gate** — drop features with `|Gini| < --min-iv` on fit (no signal).
3. **null-importance** — fit LightGBM on fit, compare each feature's gain to the
   gain it earns under `--null-runs` target shuffles; keep only features beating
   the `--null-pct` percentile of their own noise.
4. **corr cluster** — Spearman-cluster survivors at `|rho| >= --corr-max`, keep the
   strongest (by univariate Gini) per cluster.

### Usage
```bash
# reuse the OB matrix you already built; builds the cheap KG side once, then boosts
python lgbm_2/run_boost.py "C:\Users\Josep\Downloads\homecredit" --max-cases 0 \
      --reuse-ob ..\lgbm_test_run\cache\ob_matrix_full.pkl

# faster iteration (lighter search)
python lgbm_2/run_boost.py "...\homecredit" --max-cases 0 --tune-iter 15 --null-runs 10

# skip stages
python lgbm_2/run_boost.py "...\homecredit" --max-cases 0 --no-tune --null-runs 0 --no-ensemble
```

### Key flags
| flag | default | meaning |
|------|---------|---------|
| `--drift-max` | 3.0 | drop features with std_shift above this |
| `--min-iv` | 0.005 | drop features with \|univariate gini\| below this |
| `--null-runs` | 15 | target shuffles for null-importance (`0` = skip) |
| `--null-pct` | 75 | percentile of null gain a feature must beat |
| `--corr-max` | 0.90 | Spearman \|rho\| clustering threshold |
| `--tune-iter` | 25 | random-search iterations (`--no-tune` to skip) |
| `--no-ensemble` | — | skip the LGBM+XGB blend |
| `--device gpu` | cpu | GPU for LightGBM training |

### Runtime note
The one-time cost is the matrix build (reused after). On the full 1.5M, the
heaviest parts of this script are the null-importance refits (`--null-runs`) and
the tuning search (`--tune-iter`) — turn them down for a quick pass, up for a
thorough one. GPU (`--device gpu`) accelerates the LightGBM fits.

### Honest expectation
Selection/tuning/ensemble are **model-side** gains — typically a few Gini points
and a firmer stability slope. The larger remaining lever for KG→OB is
reconstruction **fidelity** (better renderers + more reconstructable source
tables); that's the next phase, not this script.


---

## UPDATE: `--frame` and the fidelity gate (post-diagnostic pivot)

The first full `run_boost` run showed **KG->OB (~0.22) sits *below* the OB->OB floor (~0.34)**: the
train/serve skew from training on raw bureau features costs more than the clean signal buys. That is a
result, not a bug — it means **OB->OB is the actually-deployable model** (reconstruct labelled training
data into the serving representation, then train + serve on it), and KG->OB is best treated as a
*diagnostic* of the representation gap.

### `--frame {kg2ob,ob2ob,kg2kg}`  (default `kg2ob`)
Picks what the whole funnel trains and serves on:
- `kg2ob` — train clean Kaggle-direct, serve OB reconstruction. **Diagnostic** of the representation gap.
- `ob2ob` — train + serve on the OB reconstruction. **The deployable model — push this one.**
- `kg2kg` — train + serve clean Kaggle-direct. The **ceiling** (not deployable; best case if reconstruction were perfect).

Selection, tuning and the ensemble all follow the chosen frame. Split IDs are still the shared KG∩OB
universe so numbers are comparable across frames.

### Fidelity gate (cross-rep only)
In `kg2ob` the selection funnel now runs **two** cross-representation gates before the usual signal gates:
- **1a) drift gate** (`--drift-max`, default 3.0) — drop features with a large mean shift KG→OB.
- **1b) fidelity gate** (`--min-fidelity-corr`, default 0.30) — drop features whose **per-applicant
  KG↔OB correlation** is low. These are the ones that don't reconstruct (matching mean, scrambled
  values) and quietly poison KG->OB at serving. This is what the plain drift gate missed.

Both are automatically skipped for `ob2ob` / `kg2kg` (train and serve share one representation, so
there is no skew to gate).

### Recommended commands
```bash
# 1) the deployable model — boost OB->OB (this is your headline going forward)
python lgbm_2/run_boost.py "C:\Users\Josep\Downloads\homecredit" --max-cases 0 --frame ob2ob \
      --reuse-ob ..\lgbm_test_run\cache\ob_matrix_full.pkl

# 2) the ceiling, for context
python lgbm_2/run_boost.py "...\homecredit" --max-cases 0 --frame kg2kg --reuse-ob ..\lgbm_test_run\cache\ob_matrix_full.pkl

# 3) the diagnostic, now with the fidelity gate telling you which features fail to reconstruct
python lgbm_2/run_boost.py "...\homecredit" --max-cases 0 --frame kg2ob --reuse-ob ..\lgbm_test_run\cache\ob_matrix_full.pkl
```
Run `run_compare.py` on the same `--max-cases` for the confirming KG->KG / OB->OB / KG->OB 2×2 plus the
drift table's `corr` column (which sets a sensible `--min-fidelity-corr` from real data).
