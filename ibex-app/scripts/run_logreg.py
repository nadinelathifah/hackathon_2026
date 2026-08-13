#!/usr/bin/env python3
"""ONE-COMMAND logistic-regression pipeline for open-banking-reconstructable
credit-risk features.

This is the robust, defensible path. It does NOT use the canonical-object adapter
or stream the 188M-row credit_bureau_a_2 into memory. Instead it reuses the exact
chunk-by-chunk aggregation that diagnose_dpd.py already proved on the real data
(mean_dpd univariate |Gini| ~= 0.32): read each bureau chunk, aggregate per
case_id immediately, discard the raw rows, combine the small per-case aggregates.
Peak memory is one chunk, so it cannot hit the earlier ArrayMemoryError.

EVERY feature is reconstructable from open-banking data (this is the parity
claim that makes the dissertation defensible):

  from bureau payment history (credit_bureau_a_2 + b_2):
    max_dpd, mean_dpd, num_dpd_gt0, num_dpd_ge30, max_overdue, total_overdue,
    num_bureau_payments
      -> open banking: DPD reconstructed from payment-vs-schedule timing, overdue
         from arrears amounts, counts from the transaction stream.
  from previous applications (applprev_1):
    total_annuity, max_annuity, num_prev_apps
      -> open banking: recurring outgoing instalments detected in transactions.
  from static (static_0):
    monthly_income  -> open banking: recurring salary credits.
  derived:
    debt_to_income = total_annuity / monthly_income.

DPD is floored at 0 and capped at 90 days (Basel default definition; also tames
the bureau's absurd b_2 raw outliers and matches the capped open-banking DPD).

Usage:
    python scripts/run_logreg.py <kaggle_dir> [--features-out feats.parquet]
                                             [--leave-one-out] [--max-files N]

  <kaggle_dir>      folder with the competition parquet files (train_base.parquet,
                    train_credit_bureau_a_2_*.parquet, ...).
  --features-out    also write the per-case feature table (parquet + csv).
  --leave-one-out   after training, print each feature's drop-column Gini impact.
  --max-files N      cap chunks per table (quick trial). Default: all.

Test-set Gini verdict: <0.02 NO SIGNAL | <0.10 WEAK | <0.40 REAL | else STRONG.
"""
from __future__ import annotations
import argparse
import glob
import os
import re
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obcredit.adapters.kaggle_adapter import _logical_stem  # noqa: E402
from obcredit.modeling.metrics import roc_auc, gini_stability  # noqa: E402

DPD_CAP = 90.0
SERIOUS_DPD = 30.0

DPD_RE = re.compile(r"dpd", re.I)
OVERDUE_RE = re.compile(r"overdue", re.I)
CASE = "case_id"

BUREAU_STEMS = ("credit_bureau_a_2", "credit_bureau_b_2")
INCOME_COL = "maininc_215A"

# The final parity-safe feature set (order fixed for reproducibility).
FEATURES = [
    "max_dpd", "mean_dpd", "num_dpd_gt0", "num_dpd_ge30",
    "max_overdue", "total_overdue", "num_bureau_payments",
    "total_annuity", "max_annuity", "num_prev_apps",
    "monthly_income", "debt_to_income",
]


# --------------------------------------------------------------------------- #
# Pure aggregation helpers (unit-tested in tests/test_logreg.py on in-memory
# frames, since the sandbox has no parquet engine).
# --------------------------------------------------------------------------- #
def _row_max(df: pd.DataFrame, cols) -> pd.Series:
    """Per-row max across the given columns, coercing text/garbage to NaN."""
    if not cols:
        return pd.Series(np.nan, index=df.index)
    num = df[cols].apply(pd.to_numeric, errors="coerce")
    return num.max(axis=1)


def agg_bureau_chunk(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate ONE bureau chunk to per-case partials. DPD is clipped to
    [0, 90] per row before aggregating. Returns a frame indexed by case_id."""
    names = list(df.columns)
    dpd_cols = [c for c in names if DPD_RE.search(c)]
    ovd_cols = [c for c in names if OVERDUE_RE.search(c)]
    dpd = _row_max(df, dpd_cols).clip(lower=0.0, upper=DPD_CAP)
    ovd = _row_max(df, ovd_cols).clip(lower=0.0)
    g = pd.DataFrame({CASE: df[CASE].astype("int64", errors="ignore"),
                      "dpd": dpd, "ovd": ovd})
    out = g.groupby(CASE).agg(
        dpd_max=("dpd", "max"),
        dpd_sum=("dpd", "sum"),
        n_pmts=("dpd", "size"),
        n_dpd_gt0=("dpd", lambda s: float((s > 0).sum())),
        n_dpd_ge30=("dpd", lambda s: float((s >= SERIOUS_DPD).sum())),
        ovd_max=("ovd", "max"),
        ovd_sum=("ovd", "sum"),
    )
    return out


def combine_bureau(partials) -> pd.DataFrame:
    """Combine per-chunk bureau partials into one row per case, then derive the
    final bureau features (true mean = summed dpd / summed payments)."""
    if not partials:
        return pd.DataFrame()
    allp = pd.concat(partials)
    comb = allp.groupby(level=0).agg(
        dpd_max=("dpd_max", "max"),
        dpd_sum=("dpd_sum", "sum"),
        n_pmts=("n_pmts", "sum"),
        n_dpd_gt0=("n_dpd_gt0", "sum"),
        n_dpd_ge30=("n_dpd_ge30", "sum"),
        ovd_max=("ovd_max", "max"),
        ovd_sum=("ovd_sum", "sum"),
    )
    n = comb["n_pmts"].replace(0, np.nan)
    out = pd.DataFrame(index=comb.index)
    out["max_dpd"] = comb["dpd_max"]
    out["mean_dpd"] = comb["dpd_sum"] / n
    out["num_dpd_gt0"] = comb["n_dpd_gt0"]
    out["num_dpd_ge30"] = comb["n_dpd_ge30"]
    out["max_overdue"] = comb["ovd_max"]
    out["total_overdue"] = comb["ovd_sum"]
    out["num_bureau_payments"] = comb["n_pmts"]
    return out


def agg_applprev_chunk(df: pd.DataFrame) -> pd.DataFrame:
    """Per-case annuity partials from one applprev_1 chunk."""
    ann_cols = [c for c in df.columns if c.startswith("annuity_")]
    ann = _row_max(df, ann_cols).clip(lower=0.0)
    g = pd.DataFrame({CASE: df[CASE].astype("int64", errors="ignore"), "ann": ann})
    return g.groupby(CASE).agg(ann_sum=("ann", "sum"),
                               ann_max=("ann", "max"),
                               n_apps=("ann", "size"))


def combine_applprev(partials) -> pd.DataFrame:
    if not partials:
        return pd.DataFrame()
    allp = pd.concat(partials)
    comb = allp.groupby(level=0).agg(total_annuity=("ann_sum", "sum"),
                                     max_annuity=("ann_max", "max"),
                                     num_prev_apps=("n_apps", "sum"))
    return comb


def assemble_features(base: pd.DataFrame, bureau: pd.DataFrame,
                      applprev: pd.DataFrame, income: pd.DataFrame) -> pd.DataFrame:
    """Left-join everything onto base (one row per applicant) and derive DTI.
    A missing bureau/applprev record => no known delinquency/obligation => 0.
    Income is left as NaN when unknown (median-imputed at train time)."""
    feats = base.copy()
    for frame in (bureau, applprev, income):
        if frame is not None and not frame.empty:
            feats = feats.join(frame, how="left")
    for c in ("max_dpd", "mean_dpd", "num_dpd_gt0", "num_dpd_ge30", "max_overdue",
              "total_overdue", "num_bureau_payments", "total_annuity",
              "max_annuity", "num_prev_apps"):
        if c not in feats:
            feats[c] = 0.0
        feats[c] = feats[c].fillna(0.0)
    if "monthly_income" not in feats:
        feats["monthly_income"] = np.nan
    inc = pd.to_numeric(feats["monthly_income"], errors="coerce")
    feats["debt_to_income"] = np.where(inc > 0, feats["total_annuity"] / inc, np.nan)
    return feats


# --------------------------------------------------------------------------- #
# Model (pure NumPy: no sklearn / xgboost needed)
# --------------------------------------------------------------------------- #
class NumpyLogReg:
    def __init__(self, lr=0.3, n_iter=1500, l2=1e-2):
        self.lr, self.n_iter, self.l2 = lr, n_iter, l2
        self.w = None
        self.b = 0.0

    def fit(self, X, y):
        n, d = X.shape
        self.w = np.zeros(d)
        self.b = 0.0
        for _ in range(self.n_iter):
            p = 1.0 / (1.0 + np.exp(-(X @ self.w + self.b)))
            g = p - y
            self.w -= self.lr * (X.T @ g / n + self.l2 * self.w)
            self.b -= self.lr * g.mean()
        return self

    def predict_proba(self, X):
        return 1.0 / (1.0 + np.exp(-(X @ self.w + self.b)))


def prepare(Xtr, Xva):
    """Median-impute (fit on train) then z-score (fit on train)."""
    med = np.nanmedian(Xtr, axis=0)
    med = np.where(np.isnan(med), 0.0, med)
    Xtr = np.where(np.isnan(Xtr), med, Xtr)
    Xva = np.where(np.isnan(Xva), med, Xva)
    mu = Xtr.mean(axis=0)
    sd = Xtr.std(axis=0)
    sd = np.where(sd < 1e-9, 1.0, sd)
    return (Xtr - mu) / sd, (Xva - mu) / sd


def fit_eval(X, y, tr, va, cols_idx):
    Xtr, Xva = prepare(X[tr][:, cols_idx], X[va][:, cols_idx])
    m = NumpyLogReg().fit(Xtr, y[tr])
    return 2 * roc_auc(y[va], m.predict_proba(Xva)) - 1


def verdict(g):
    return ("NO SIGNAL" if g < 0.02 else "WEAK" if g < 0.10
            else "REAL SIGNAL" if g < 0.40 else "STRONG")


# --------------------------------------------------------------------------- #
# File IO (mirrors diagnose_dpd.py, which already works on the real data)
# --------------------------------------------------------------------------- #
def _group_files(path):
    out = {}
    for fp in sorted(glob.glob(os.path.join(path, "*.parquet"))):
        s = _logical_stem(os.path.splitext(os.path.basename(fp))[0])
        if s:
            out.setdefault(s, []).append(fp)
    return out


def _schema_names(fp):
    import pyarrow.parquet as pq
    return list(pq.ParquetFile(fp).schema.names)


def build_features(path, max_files=None):
    files = _group_files(path)
    print("tables found:", {k: len(v) for k, v in files.items()})

    # base (targets + week); required.
    base_files = files.get("base", [])
    if not base_files:
        raise FileNotFoundError("no base parquet (train_base.parquet) found")
    bparts = []
    for fp in base_files:
        names = _schema_names(fp)
        cols = [c for c in (CASE, "WEEK_NUM", "target") if c in names]
        bparts.append(pd.read_parquet(fp, columns=cols))
    base = pd.concat(bparts, ignore_index=True)
    base[CASE] = base[CASE].astype("int64", errors="ignore")
    base = base.set_index(CASE)
    print(f"base: {len(base):,} applicants")

    # bureau (chunk-by-chunk; peak memory = one chunk)
    partials = []
    for stem in BUREAU_STEMS:
        flist = files.get(stem, [])
        if max_files:
            flist = flist[:max_files]
        for fp in flist:
            names = _schema_names(fp)
            use = [CASE] + [c for c in names if DPD_RE.search(c) or OVERDUE_RE.search(c)]
            use = [c for c in dict.fromkeys(use) if c in names]
            if use == [CASE]:
                print(f"  [{stem}] {os.path.basename(fp)}: no dpd/overdue cols -> skip")
                continue
            df = pd.read_parquet(fp, columns=use)
            partials.append(agg_bureau_chunk(df))
            print(f"  streamed {os.path.basename(fp)} rows={len(df):,}")
            del df
    bureau = combine_bureau(partials)
    print(f"bureau features: {len(bureau):,} cases with a bureau record")

    # applprev annuity
    ap_partials = []
    aplist = files.get("applprev_1", [])
    if max_files:
        aplist = aplist[:max_files]
    for fp in aplist:
        names = _schema_names(fp)
        ann_cols = [c for c in names if c.startswith("annuity_")]
        if not ann_cols:
            continue
        df = pd.read_parquet(fp, columns=[CASE] + ann_cols)
        ap_partials.append(agg_applprev_chunk(df))
        del df
    applprev = combine_applprev(ap_partials)
    print(f"applprev features: {len(applprev):,} cases")

    # income from static_0
    inc_partials = []
    for fp in files.get("static_0", []):
        names = _schema_names(fp)
        if INCOME_COL not in names:
            continue
        df = pd.read_parquet(fp, columns=[CASE, INCOME_COL])
        df[CASE] = df[CASE].astype("int64", errors="ignore")
        inc_partials.append(df.groupby(CASE)[INCOME_COL].max())
        del df
    income = pd.DataFrame()
    if inc_partials:
        income = pd.concat(inc_partials).groupby(level=0).max().to_frame("monthly_income")
    print(f"income: {len(income):,} cases")

    feats = assemble_features(base, bureau, applprev, income)
    return feats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kaggle_dir")
    ap.add_argument("--features-out", default=None)
    ap.add_argument("--leave-one-out", action="store_true")
    ap.add_argument("--max-files", type=int, default=None)
    args = ap.parse_args()

    print("=== obcredit run_logreg (parity-safe features, no a_2 streaming) ===")
    feats = build_features(args.kaggle_dir, max_files=args.max_files)

    if "target" not in feats.columns:
        print("no target column in base -> cannot train")
        sys.exit(1)
    feats = feats[~feats["target"].isna()]

    cols = [c for c in FEATURES if c in feats.columns]
    X = feats[cols].to_numpy(dtype=float)
    y = feats["target"].to_numpy(dtype=float)
    week = (feats["WEEK_NUM"].to_numpy(dtype=float) if "WEEK_NUM" in feats.columns
            else np.zeros(len(y)))

    if args.features_out:
        outp = args.features_out
        feats[cols].to_parquet(outp)
        feats[cols].to_csv(os.path.splitext(outp)[0] + ".csv")
        print(f"wrote features -> {outp} (+ .csv): {feats.shape[0]:,} x {len(cols)}")

    # out-of-time split (train on earlier weeks, test on latest 20%)
    cut = np.quantile(week, 0.8)
    tr, va = week <= cut, week > cut
    if tr.sum() == 0 or va.sum() == 0:
        rng = np.random.default_rng(0)
        idx = rng.permutation(len(y))
        va = np.zeros(len(y), bool)
        va[idx[: max(1, len(y) // 5)]] = True
        tr = ~va

    print(f"\nrows={len(y):,}  default={y.mean():.3%}  "
          f"train={int(tr.sum()):,}  test={int(va.sum()):,}  features={len(cols)}")

    Xtr, Xva = prepare(X[tr], X[va])
    model = NumpyLogReg().fit(Xtr, y[tr])
    auc_tr = roc_auc(y[tr], model.predict_proba(Xtr))
    auc_va = roc_auc(y[va], model.predict_proba(Xva))
    g_va = 2 * auc_va - 1
    stab = gini_stability(week[va], y[va], model.predict_proba(Xva))
    print(f"TRAIN  AUC {auc_tr:.4f}  Gini {2*auc_tr-1:.4f}")
    print(f"TEST   AUC {auc_va:.4f}  Gini {g_va:.4f}")
    print(f"gini_stability {stab['metric']:.4f} "
          f"(mean {stab['mean_gini']:.4f}, slope {stab['slope']:.5f})")
    print(f"VERDICT: {verdict(g_va)}")

    print("\nunivariate |Gini| per feature:")
    for gg, c in sorted(((abs(2*roc_auc(y, np.nan_to_num(X[:, j])) - 1), cols[j])
                         for j in range(len(cols))), reverse=True):
        print(f"  {c:22s} {gg:.4f}")

    print("\ncoefficients (standardised; sign = risk direction):")
    for c, w in sorted(zip(cols, model.w), key=lambda t: -abs(t[1])):
        print(f"  {c:22s} {w:+.4f}")

    if args.leave_one_out:
        print("\nleave-one-out importance (Gini drop when feature removed):")
        full = list(range(len(cols)))
        base_g = fit_eval(X, y, tr, va, full)
        print(f"  full model test Gini = {base_g:.4f}")
        for d, c in sorted(((base_g - fit_eval(X, y, tr, va, [k for k in full if k != j]), cols[j])
                            for j in range(len(cols))), reverse=True):
            flag = "  <- drop candidate" if d <= 0 else ""
            print(f"  {c:22s} {d:+.4f}{flag}")


if __name__ == "__main__":
    main()
