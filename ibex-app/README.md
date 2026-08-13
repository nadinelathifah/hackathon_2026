# ibex_scale -- full-population retrain + cohort testing

Three scripts. They do **not** replace BUILD 18 and they do **not** change any
feature definition. Same `f()`, same adapters, same registry, so `ob -> ob`
stays a like-for-like comparison against your existing numbers.

| file | what it does |
|---|---|
| `diagnose_rows.py` | tells you the honest row ceiling **before** you rebuild |
| `build_ob_full.py` | builds the OB matrix over the whole population, in resumable shards |
| `cohort_report.py` | scores 100+ random eval-slice applicants and characterises each one |

## Install

Drop all three into `step3\scripts\`:

```powershell
$src = "$env:USERPROFILE\Downloads\ibex_scale"
$dst = "C:\Users\Josep\Downloads\step3_build18\step3\scripts"
Copy-Item "$src\diagnose_rows.py"  $dst -Force
Copy-Item "$src\build_ob_full.py"  $dst -Force
Copy-Item "$src\cohort_report.py"  $dst -Force
```

If you unzipped somewhere else, adjust `$src`. Nothing else needs editing.

---

## Step 1 -- find the real ceiling (2 minutes)

```powershell
cd C:\Users\Josep\Downloads\step3_build18\step3
py -3.13 scripts\diagnose_rows.py "C:\Users\Josep\Downloads\homecredit" --cache-dir "C:\Users\Josep\Downloads\obcache"
```

**STOP and read the VERDICT block before going further.** It prints:

- **labelled universe** -- rows that have a `target`. This is your true ceiling.
  Only `base*.parquet` carries labels, so if that file is missing this number
  collapses and the fix is a re-download, not a code change.
- **adapter will stream** -- how many case_ids the adapter can actually produce.
- **largest matrix built so far** -- should be ~305,332.

If labelled universe is 500k+, continue. If it is close to 305k, the 305k was
not an accident and you should quote that number in the write-up rather than
chasing 1.5M.

---

## Step 2 -- build the full matrix (hours, interruptible)

```powershell
py -3.13 scripts\build_ob_full.py "C:\Users\Josep\Downloads\homecredit" --cache-dir "C:\Users\Josep\Downloads\obcache" --resume
```

It writes `obcache\ob_shards\ob_shard_00000.pkl`, `00001`, ... plus a manifest,
flushing every 150,000 rows so RAM stays flat. **This is the fix for the crash
that killed your PC last time** -- that run tried to hold everything in memory.

Safe to Ctrl-C. Re-run the identical command and it resumes from the manifest.

Tuning if it is slow or heavy:

```powershell
--workers 6          # default is cores-1; lower it if the machine is unusable
--shard-size 100000  # smaller shards = lower peak RAM
```

**Watch out:** if you paste a path ending in a curly quote the argument silently
becomes a different string and it rebuilds from scratch. Retype the quotes.

---

## Step 3 -- merge the shards (minutes)

```powershell
py -3.13 scripts\build_ob_full.py "C:\Users\Josep\Downloads\homecredit" --cache-dir "C:\Users\Josep\Downloads\obcache" --merge-only
```

Produces `obcache\ob_matrix_full_all.pkl`. **The line that matters is
`LABELLED rows`** -- that is what actually trains. Unlabelled rows are carried
but dropped by the split.

---

## Step 4 -- retrain and recalibrate

```powershell
py -3.13 scripts\calibrate_score.py "C:\Users\Josep\Downloads\homecredit" --max-cases 0 --cache-dir "C:\Users\Josep\Downloads\obcache" --reuse-ob "C:\Users\Josep\Downloads\obcache\ob_matrix_full_all.pkl"
```

`--reuse-ob` bypasses the build-stamp check, so this loads instantly and only
trains. Record four numbers from the output:

1. `[calibrate] frame=ob rows=` -- the new population
2. `lower_plateau_knots` and the zero-default block size
3. `observed_default_rate` -- the **new** base rate, replaces 0.03871
4. `eval gini raw / calib`

Then re-derive the floor with the numbers you just got:

```powershell
py -3.13 scripts\evidence.py tail --n <new block size> --base-rate <new base rate>
```

**Do not keep `pd_floor = 0.0141`.** It was derived from n=175. With ~875 in the
block the defensible floor drops to roughly 0.0040 and the ceiling rises from
672 to about 746, which is the first point at which band A becomes reachable.
Re-run `fixfloor.py` with the new value.

---

## Step 5 -- test 150 different people

```powershell
py -3.13 scripts\cohort_report.py --ob "C:\Users\Josep\Downloads\obcache\ob_matrix_full_all.pkl" -n 150 --csv artifacts\cohort.csv
```

This is the direct answer to *"how do I know a different person gets a better
score?"*. It samples 150 applicants at random from the **eval slice** -- the
last 20% by competition week, never seen in training or calibration -- and
prints for each one a score, a band, and a plain-English description built only
from open-banking features:

```
   case_id  score bd      PD   obl   income  description
   2847193  681.4  B  0.0139     7     3120  thick / long clean / comfortable
   1938472  604.2  C  0.0402     2     1890  thin / clean / comfortable
    884712  551.8  D  0.0771     4     2240  medium / minor late / stretched
    229384  498.1  E  0.1284     3      980  medium / serious arrears / stretched
```

Then it groups by **file thickness**, **payment conduct** and **affordability**
and shows mean/min/max score plus the observed default rate per group.

Three things to check in that summary:

- thick+clean out-scores thin, and thin out-scores arrears;
- `obs dflt` falls as mean score rises -- that is the model working;
- **if `no-file` scores above `thick / long clean`, missingness is being
  rewarded.** That is the median-fill bug in `_prep_lgbm`, and this report is
  how you would catch it.

Use `--seed 7`, `--seed 99` etc. to confirm the pattern is not one lucky draw.

---

## Why this is `ob -> ob` and not something else

`build_ob_full.py` uses the identical path `run_compare.py` uses for the OB
side: canonical -> ground truth -> `to_truelayer_payloads` -> `TrueLayerAdapter`
-> canonical -> `FeaturePipeline`. Feature vectors are therefore produced by the
same reconstruction that runs when a real TrueLayer connection comes in.

`cohort_report.py` scores through the exact serving path -- persisted training
medians, `num_iteration=best_iteration`, the pickled calibrator, then
`pd_to_score`. If it points at a `kg`-frame artifact it warns you, because that
would be measuring a model you do not deploy.

## After any of this: re-run the tests

```powershell
py -3.13 run_tests.py
```

All 51 should pass. None of these scripts touch feature definitions, so parity
must be unaffected -- if it is not, something else changed.
