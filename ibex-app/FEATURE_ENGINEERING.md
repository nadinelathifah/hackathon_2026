# Feature Engineering & Selection Plan

Reference doc for pushing Gini + the competition **stability score** as high as an
open-banking-reconstructable model honestly can. Everything here respects the
core constraint: the SAME `f()` runs at train (Kaggle raw) and inference
(TrueLayer live), so a feature only counts if it can be rebuilt from open
banking.

---

## 0. What we are actually scoring

The leaderboard is **not** raw Gini. It is a stability-penalised metric:

```
stability = mean(weekly_gini) + 88 * min(0, slope(weekly_gini)) - 0.5 * std(residuals)
```

- `mean(weekly_gini)` -> raw discrimination (rewards signal).
- `88 * min(0, slope)` -> huge penalty if Gini **falls** over weeks (rewards not drifting).
- `-0.5 * std(residuals)` -> penalty for jumpy week-to-week performance.

Strong published solutions score ~**0.59-0.62** (using ALL tables incl. bureau +
demographics). Our **open-banking-only** reconstructable ceiling is roughly:
raw Gini ~**0.50-0.55**, stability-metric ~**0.40-0.48**. We already have a
linear-model OB Gini of **0.4585** on 1.53M applicants. 0.7 is a full-data,
all-table number and is **not** an OB-only target -- chasing it invites leakage.

Implication for FE: every technique below is tagged for whether it mainly buys
**[Gini]** (discrimination) or **[stability]** (drift/variance), because the
metric pays for both.

---

## 1. Parity discipline (the gate that everything passes through)

- **[parity-safe]** features reconstruct identically from OB and Kaggle. These
  are the ONLY features compared in `run_step3` fidelity and the ONLY features
  fed to the parity-only `run_xgb`.
- **[OB-native]** features (`parity=False`) cannot be built from the Kaggle
  bureau grid (e.g. anything using real transaction timing or balances). They
  are legitimate deployable signal but live in a separate group and prove open
  banking's *added* value; they are excluded from the Kaggle-vs-OB comparison.
- **[low-fidelity]** parity features (the overdue-**amount** family, corr ~0.05)
  are kept but auto-flagged; expect them pruned.

A feature that does not reconstruct is noise-at-inference no matter how strong it
looks in training. Fidelity is checked BEFORE importance.

---

## 2. Feature techniques (by tier)

### Tier 1 - Breadth: depth-aware aggregation grammar  [parity-safe - Gini]
For each high-fidelity per-payment stream, group by `case_id` and emit
`count / mean / max / min / std / sum / median / nonzero` (+ `first / last`
where element order is itself parity-safe).
- Streams: **dpd** (parity-safe), **instalment** (parity-safe, per-line),
  **payment interval** (OB-native -> `parity=False`, timing).
- Turns a handful of hand-written delinquency features into ~35+ parity
  features now; the full 100-150 arrives once Tiers 2-4 are added.
- Trees split on columns -> more informative columns -> deeper ensembles (fixes
  the `best_iteration=6` starvation). **STATUS: IMPLEMENTED (BUILD 11).**

### Tier 2 - Temporal / trajectory  [parity-safe - Gini + stability]
"Getting worse" beats "level".
- Trend slope + **acceleration** (1st & 2nd difference) of DPD.
- Recent-vs-older windows (last 3/6m vs 7-24m) as ratio AND delta.
- Recency: cycles since last delinquency / missed / overdue.
- Streaks / run-length: longest worsening streak; current clean streak.
- **Lag-1/2/3 autocorrelation on the binary late-payment indicator** (the robust
  replacement for per-row AR/Fourier; built on DPD *events*, which reconstruct
  well, not DPD magnitude).
- Volatility & entropy of the payment-state sequence.

### Tier 3 - Cashflow & balance  [OB-native - Gini]
Wire up the `parity=False` stubs.
- Buffer/resilience: min balance, days below threshold, month-end trend.
- Income stability: CV of salary-credit gaps, # income sources, salary trend.
- Net cashflow / savings rate + volatility.

### Tier 4 - OB-native distress signals  [OB-native - Gini]  (novel contribution)
- Returned direct-debit / NSF count.
- Overdraft usage depth & frequency.
- Gambling / high-risk merchant share of outflow.
- New-credit-take-on velocity (loan stacking).
- Spend concentration (essential vs discretionary).

### Tier 5 - Encoding & interactions  [Gini]
- Out-of-fold WoE / target encoding (obligation kind, income-source type) w/ smoothing.
- Hand-crafted ratios/interactions: DTI x deterioration-trend, utilisation x recency,
  income-volatility x buffer.
- Monotonic WoE binning -> scorecard companion (FCA / Consumer Duty explainability).

### Tier 6 - Robustness & thin-file  [stability]
- Rank / quantile transforms -> resist week-to-week drift.
- Missingness / thin-file flags: `n_obligations`, history length, per-feature null flags.

---

## 3. Feature selection funnel (which to keep, which are noise)

Run cheap->rigorous; do EVERY decision inside CV folds (never on the final test).

0. **Structural filters** - drop near-zero-variance, >95-99% null, exact duplicates.
1. **Fidelity gate** - drop/flag parity features with low OB<->Kaggle corr (overdue-amount family).
2. **Univariate sanity** (coarse rank, NOT a decision) - per-feature Gini / Information Value.
   IV: <0.02 useless, 0.02-0.1 weak, 0.1-0.3 medium, 0.3-0.5 strong, >0.5 suspect leakage.
3. **Null-importance test** (the real noise test) - shuffle the target many times,
   retrain, build each feature's NULL importance distribution; keep a feature only
   if its real importance is in the far tail (>95th/99th pct) of its own null.
4. **Correlation-cluster dedupe** - cluster by Spearman, keep the highest-null-importance
   representative per cluster (fixes permutation understating collinear DPD duplicates).
5. **Recursive elimination on the stability metric** (CV) - drop weakest, re-score,
   stop when the penalised metric degrades. Fewer features also helps `-0.5*std`.
6. **Stability-specific pruning** - adversarial validation (drop features that predict
   the week) + per-week importance variance / PSI.

---

## 4. Model-technique roadmap (after breadth)

- Keep XGBoost for now; add breadth FIRST, then raise `max_depth` (breadth unlocks depth).
- Custom **stability `feval`** for early-stopping / model-selection (metric is
  non-differentiable -> cannot be the training loss). Gate it ON only on full data
  (weekly slices at 25k are too thin/noisy).
- Migrate primary model to **LightGBM**; add **CatBoost**; **voting/blend ensemble**.
- Grouped / time-based CV by week. Hyperparameter tuning AFTER breadth. Isotonic/Platt calibration.
- Seed-averaging / bagging to shrink prediction variance -> helps the stability std term.
- Segmented thin-file / thick-file models.

---

## 5. Build order

1. **Tier 1** aggregation grammar (parity-safe)                 <- IMPLEMENTED (BUILD 11)
2. **Tier 2** temporal / trajectory (parity-safe)
3. Adversarial-validation drift report + selection funnel harness
4. **Tiers 3-4** OB-native cashflow + distress signals (and an OB-native model path)
5. Encoding / interactions
6. Full-population validation + stability `feval` + LightGBM/ensemble

---

## Changelog

- **BUILD 11** - Tier 1 depth-aware aggregation grammar added
  (`obcredit/feature_aggregations.py`): dpd + instalment streams (parity-safe),
  interval stream (OB-native). Reconstruction of existing features unchanged.

---

## 6. Production architecture notes (future; captured for reference)

### 6.1 Performance / RAPIDS cuDF (WSL + GPU)
- The current bottleneck is NOT pandas per se: it is the per-applicant,
  object-oriented pipeline (adapters build CanonicalApplicant objects;
  FeatureContext runs pure-Python per row). The 25k run spends ~30s per 4k-case
  Kaggle batch there, not in dataframe ops.
- So cuDF is NOT a drop-in speedup for today's code: it accelerates VECTORISED
  columnar ops, not Python object loops. To benefit from RAPIDS we must rewrite
  f() as a vectorised, columnar feature builder (group-by aggregations over a
  payments table keyed by case_id) instead of per-row Python.
- Plan: keep the OO pipeline as the readable, parity-checked "golden" f(); build
  a cuDF/vectorised twin for production scale; add a test asserting the
  vectorised build matches the OO build bit-for-bit (parity discipline extended
  to the fast path).
- Interim CPU speedups without a rewrite: multiprocessing over case_id batches,
  caching, narrower dtypes. Not needed until we run full 1.5M repeatedly.

### 6.2 Robustness to missing / partial open-banking data
The inference path must degrade gracefully -- a live OB connection is often
partial (few months, one account, no salary detected).
- **Won't crash:** feature fns already return 0.0/None on empty streams; the
  pipeline catches per-feature errors -> None; the model imputes (train-median).
- **But absence != low risk:** missing obligations make an applicant LOOK clean.
  Mitigate with:
  - **Missingness flags** (Tier 6): 0/1 per feature-group that was unavailable.
  - **Coverage / confidence score:** months of history, # accounts linked,
    salary detected? Low coverage -> lower decision confidence / manual review,
    never an automatic "clean".
  - **Provenance per feature:** observed | imputed | declared, kept for audit.
- **Redundancy by design:** triangulate each construct from multiple sources so
  one gap doesn't zero the signal (income = detected salary inflow OR averaged
  regular credits OR declared; obligations = detected streams OR direct-debit /
  standing-order mandates OR declared).

### 6.3 Pre-application survey (declared data) fallback
- Use CanonicalApplicant.declared as the fallback layer. A short questionnaire
  (declared income, known obligations, dependents, housing cost) fills what OB
  cannot see.
- Precedence: OB-observed > declared; but keep BOTH -- a declared-vs-detected
  MISMATCH is itself a risk/fraud feature (declared income >> detected credits).
- Also expected for FCA CONC affordability: you cannot lean on incomplete data
  alone; a verified-plus-declared blend with documented handling is defensible.
