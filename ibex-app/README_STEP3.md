# Step 3 — Kaggle → open-banking round-trip (defensible feature reconstruction)

**BUILD 8 — real per-payment, no bucketing.**

This folder proves the central dissertation claim end-to-end: **the same function
`f()` builds the same credit-risk features whether it is fed Kaggle bureau data
(training) or TrueLayer open-banking transactions (inference).** It takes the
*real* Kaggle data we already model on, reshapes it into the exact open-banking
format we would inherit live, reconstructs the features from that format, and
checks they match the Kaggle-direct features.

## What changed in BUILD 8 (and why it matters)

BUILD 7 built the open-banking ground truth from a per-case **summary** — the
missed/late cycles were *synthesised* to reproduce summary counts on a single
obligation. That lossy `summary → one obligation` step (not open banking) was
the main thing depressing the reconstructed Gini to ~0.16 on 25k. It was a
**floor**, not the open-banking ceiling.

BUILD 8 drives the whole round-trip from the **real per-payment history**:

- `KaggleAdapter.stream_canonical()` yields real `CanonicalApplicant`s whose
  bureau obligations carry **every payment's own reported DPD + overdue**.
- `canonical_to_ground_truth(...)` copies those figures verbatim into
  `GTObligation.dpd_seq` / `overdue_seq` — **no bucketing**.
- Both shapes are rendered from that one ground truth, so the **only** surviving
  source-difference is open banking's DPD-from-timing limit (a payment ≥15 days
  late is indistinguishable from a missed direct debit → rendered *absent* →
  imputed by the adapter).

## What "step 1" and "step 2" mean here

- **Step 1 — model.** Train the logistic regression against the real target with
  an out-of-time split by competition week, on **both** matrices, and print each
  Gini + verdict. `run_step3.py` now reports:
  - **Kaggle-direct Gini** = the real feature power of the 12 behaviours
  - **Open-banking Gini** = the fair reconstruction estimate
  - **the gap / % retained** = the measured cost of open banking
- **Step 2 — fidelity.** Compare the open-banking-reconstructed features against
  the Kaggle-direct features, per parity feature (match rate, mean abs diff,
  correlation).

## How the round-trip works

```
real Kaggle parquet
      │  KaggleAdapter.stream_canonical()  (memory-safe; 188M-row bureau never held in RAM)
      ▼
real CanonicalApplicant  (every payment keeps its OWN reported DPD + overdue)
      │  canonical_to_ground_truth()  — verbatim, NO bucketing
      ▼
GTApplicant  (one neutral description of the real history)
                    ┌───────────────────┴───────────────────┐
                    ▼                                         ▼
   to_kaggle_frames(...)                       to_truelayer_payloads(...)
   (bureau shape, DPD read directly)           (OB transactions, DPD reconstructed)
                    │                                         │
            KaggleAdapter                             TrueLayerAdapter
                    │                                         │
                    └──────────── SAME FeaturePipeline ───────┘
                                        │
                        step 2: compare │  step 1: model on BOTH matrices
```

## Run the test suite (works anywhere — no parquet/sklearn needed)

```
python run_tests.py
```

Expected: `24/24 tests passed … ALL GREEN`. The seven step-3 tests prove:

| test | proves |
|------|--------|
| `test_roundtrip_parity_features_match` | OB-reconstructed == Kaggle-direct on every parity feature (on-time, missed **and** small-late cycles) |
| `test_arrears_payer_is_riskier_both_sources` | delinquency ordering is preserved on both renderings |
| `test_logreg_recovers_signal_from_open_banking` | a logistic regression on OB-reconstructed features recovers strong signal (Gini > 0.40) |
| `test_synthesis_rules` | the legacy Kaggle-summary → ground-truth reverse map still obeys its rules |
| `test_summarize_to_applicants_shapes` | the summary → applicant builder produces the right structures |
| `test_real_per_cycle_roundtrip_parity` | **the real per-payment path (`dpd_seq`, no bucketing) round-trips identically on both sources** |
| `test_canonical_to_ground_truth_carries_real_dpd` | **`canonical_to_ground_truth` copies each payment's reported DPD/overdue verbatim** |

## Run on the real Kaggle download

```
python scripts/run_step3.py "C:\\Users\\Josep\\Downloads\\homecredit" --leave-one-out
```

Options: `--max-cases N` (default 25000; use `0` for all — note the real
per-payment path retains the sampled cases in memory, so keep this bounded),
`--batch B` (cases per render/reconstruct batch — memory bound). It prints the
Step 2 fidelity table and the Step 1 Kaggle-direct vs open-banking Ginis + gap.

## Honest caveats (for the write-up)

1. **DPD-from-timing is the one real source-difference.** Open banking sees only
   transaction timing, so a payment ≥~15 days late (or missed) is
   indistinguishable from a failed direct debit; it is rendered absent and
   imputed to a capped DPD + overdue. Sub-15-day lateness the schedule model
   recovers directly. This is a genuine open-banking limit, not a modelling
   shortcut — Step 2 *quantifies* the residual (≈76–80% match on the DPD-timing
   family) rather than hiding it. Amounts/income/affordability transfer exactly.
2. **History is capped at 22 monthly cycles** so every rendered payment stays
   inside the 24-month feature window; only the most recent cycles are kept.
3. **A representative monthly instalment** (median of the applicant's applprev
   annuities) is applied per obligation so affordability renders parity-
   consistently; delinquency still carries the true per-payment figures.
4. **Fidelity here is engine/round-trip fidelity** (the *mechanism* reconstructs
   faithfully). Fidelity against a *live* TrueLayer feed still requires real
   connected accounts and is future work.
5. **The 25k open-banking Gini is a lower bound on a small sample**, not the
   ceiling of the approach — the real feature power is the Kaggle-direct Gini
   printed alongside it, and the full-population `run_logreg.py` reaches Test
   Gini ≈ 0.46 on the same 12 behaviours.

## Files added on top of the proven engine

```
step3lib/ground_truth.py   source-neutral payment history w/ real dpd_seq/overdue_seq
step3lib/renderers.py      to_kaggle_frames(...) + to_truelayer_payloads(...)
step3lib/kaggle_stream.py  canonical_to_ground_truth (real path) + legacy summary path
step3lib/model.py          NumPy logistic regression + out-of-time eval + leave-one-out
scripts/run_step3.py       one-command real-data run (step 1 on BOTH matrices + step 2)
tests/test_step3.py        the green test suite (24/24)
```
