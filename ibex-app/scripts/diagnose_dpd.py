#!/usr/bin/env python3
"""READ-ONLY inspector: where does the delinquency signal actually live?

This bypasses the feature pipeline entirely. It opens the raw Home Credit files,
prints the exact columns and how populated the DPD / overdue / annuity columns
are, then builds simple per-case aggregates DIRECTLY from those raw columns and
scores each one's univariate Gini against the real target. If a raw column
carries signal, we see it here -- no adapter, no windowing, no assumptions.

Usage:
    python scripts/diagnose_dpd.py <kaggle_dir> [max_files_per_table]

<kaggle_dir>  folder with the parquet files (train_base.parquet, etc.).

Nothing is written. Paste the whole output back.
"""
from __future__ import annotations
import glob
import os
import re
import sys

import numpy as np
import pandas as pd

# tables we care about for delinquency + affordability
STEMS = [
    "base",
    "credit_bureau_a_2", "credit_bureau_b_2",
    "credit_bureau_a_1", "credit_bureau_b_1",
    "applprev_1", "static_0", "static_cb_0",
]

DPD_RE = re.compile(r"dpd", re.I)
OVERDUE_RE = re.compile(r"overdue", re.I)
ANNUITY_RE = re.compile(r"annuity|instal|pmtscount|outstand|debt", re.I)
DATE_RE = re.compile(r"date", re.I)
YEAR_RE = re.compile(r"year", re.I)
MONTH_RE = re.compile(r"month", re.I)


def logical(stem: str):
    for pre in ("train_", "test_"):
        if stem.startswith(pre):
            stem = stem[len(pre):]
    for s in sorted(STEMS, key=len, reverse=True):
        if stem == s or stem.startswith(s + "_"):
            tail = stem[len(s):]
            if tail == "" or tail.lstrip("_").isdigit():
                return s
    return None


def group_files(path: str):
    out = {}
    for fp in sorted(glob.glob(os.path.join(path, "*.parquet"))):
        s = logical(os.path.splitext(os.path.basename(fp))[0])
        if s:
            out.setdefault(s, []).append(fp)
    return out


def schema_names(fp: str):
    import pyarrow.parquet as pq
    return list(pq.ParquetFile(fp).schema.names)


def col_stats(series: pd.Series):
    s = pd.to_numeric(series, errors="coerce")
    n = len(s)
    nn = int(s.notna().sum())
    nz = int((s.fillna(0) != 0).sum())
    out = {"n": n, "non_null": nn, "null_pct": 100.0 * (n - nn) / n if n else 0.0,
           "nonzero": nz}
    if nn:
        out.update(min=float(s.min()), max=float(s.max()), mean=float(s.mean()))
    return out


def auc(y: np.ndarray, x: np.ndarray) -> float:
    """Rank-based AUC (ties averaged); NaN in x dropped."""
    m = ~np.isnan(x)
    y = y[m]
    x = x[m]
    npos = float((y == 1).sum())
    nneg = float((y == 0).sum())
    if npos == 0 or nneg == 0:
        return 0.5
    r = pd.Series(x).rank().to_numpy()
    return (r[y == 1].sum() - npos * (npos + 1) / 2.0) / (npos * nneg)


def gini(y, x):
    return 2.0 * auc(y, x) - 1.0


# --------------------------------------------------------------------------- #
def inspect_schema(files_by_stem):
    print("=" * 78)
    print("PART 1  --  raw column inventory (DPD / overdue / annuity / timing)")
    print("=" * 78)
    for stem in ["credit_bureau_a_2", "credit_bureau_b_2", "credit_bureau_a_1",
                 "credit_bureau_b_1", "applprev_1"]:
        files = files_by_stem.get(stem)
        if not files:
            print(f"\n[{stem}]  NO FILES FOUND")
            continue
        names = schema_names(files[0])
        print(f"\n[{stem}]  {len(files)} file(s), {len(names)} columns")
        cand = [c for c in names if DPD_RE.search(c) or OVERDUE_RE.search(c)
                or ANNUITY_RE.search(c) or DATE_RE.search(c)
                or YEAR_RE.search(c) or MONTH_RE.search(c)]
        print("  candidate cols:", cand if cand else "(none matched)")
        # populate stats from the first chunk only (fast)
        stat_cols = [c for c in names if DPD_RE.search(c) or OVERDUE_RE.search(c)
                     or ANNUITY_RE.search(c)]
        if stat_cols:
            df = pd.read_parquet(files[0], columns=stat_cols)
            for c in stat_cols:
                st = col_stats(df[c])
                extra = (f" min={st['min']:.1f} max={st['max']:.1f} mean={st['mean']:.2f}"
                         if "mean" in st else "")
                print(f"    {c:34s} null={st['null_pct']:5.1f}%  "
                      f"nonzero={st['nonzero']:>8d}{extra}")


def pick(names, *regexes, exclude=()):
    out = []
    for c in names:
        if any(x in c for x in exclude):
            continue
        if any(r.search(c) for r in regexes):
            out.append(c)
    return out


def aggregate_bureau(files_by_stem, max_files):
    """Per-case max/mean DPD + overdue, aggregated straight from raw rows."""
    partials = []
    for stem in ("credit_bureau_a_2", "credit_bureau_b_2"):
        files = files_by_stem.get(stem, [])[:max_files]
        if not files:
            continue
        names = schema_names(files[0])
        dpd_cols = pick(names, DPD_RE)
        ovd_cols = pick(names, OVERDUE_RE)
        use = ["case_id"] + dpd_cols + ovd_cols
        use = [c for c in use if c in names]
        if not dpd_cols and not ovd_cols:
            print(f"  [{stem}] no dpd/overdue cols -> skipped")
            continue
        print(f"  [{stem}] dpd={dpd_cols} overdue={ovd_cols}")
        for fp in files:
            df = pd.read_parquet(fp, columns=use)
            df["case_id"] = df["case_id"].astype("int64", errors="ignore")
            row_dpd = (df[dpd_cols].apply(pd.to_numeric, errors="coerce").max(axis=1)
                       if dpd_cols else pd.Series(np.nan, index=df.index))
            row_ovd = (df[ovd_cols].apply(pd.to_numeric, errors="coerce").max(axis=1)
                       if ovd_cols else pd.Series(np.nan, index=df.index))
            g = pd.DataFrame({"case_id": df["case_id"],
                              "dpd": row_dpd, "ovd": row_ovd})
            agg = g.groupby("case_id").agg(
                max_dpd=("dpd", "max"),
                mean_dpd=("dpd", "mean"),
                num_dpd_gt0=("dpd", lambda s: float((s > 0).sum())),
                num_dpd_ge30=("dpd", lambda s: float((s >= 30).sum())),
                max_overdue=("ovd", "max"),
                total_overdue=("ovd", "sum"),
            )
            partials.append(agg)
    if not partials:
        return pd.DataFrame()
    allp = pd.concat(partials)
    comb = allp.groupby(level=0).agg(
        max_dpd=("max_dpd", "max"),
        mean_dpd=("mean_dpd", "mean"),
        num_dpd_gt0=("num_dpd_gt0", "sum"),
        num_dpd_ge30=("num_dpd_ge30", "sum"),
        max_overdue=("max_overdue", "max"),
        total_overdue=("total_overdue", "sum"),
    )
    return comb


def aggregate_annuity(files_by_stem, max_files):
    files = files_by_stem.get("applprev_1", [])[:max_files]
    if not files:
        return pd.DataFrame()
    names = schema_names(files[0])
    ann = [c for c in names if c.startswith("annuity_")] or pick(names, re.compile(r"annuity"))
    if not ann:
        print("  [applprev_1] no annuity col found")
        return pd.DataFrame()
    print(f"  [applprev_1] annuity cols={ann}")
    use = ["case_id"] + ann
    parts = []
    for fp in files:
        df = pd.read_parquet(fp, columns=[c for c in use if c in names])
        df["case_id"] = df["case_id"].astype("int64", errors="ignore")
        df["ann"] = df[ann].apply(pd.to_numeric, errors="coerce").max(axis=1)
        parts.append(df.groupby("case_id").agg(total_annuity=("ann", "sum"),
                                                max_annuity=("ann", "max")))
    allp = pd.concat(parts)
    return allp.groupby(level=0).agg(total_annuity=("total_annuity", "sum"),
                                     max_annuity=("max_annuity", "max"))


def load_base(files_by_stem):
    files = files_by_stem.get("base", [])
    if not files:
        print("NO base table found -- cannot score against target")
        return None
    names = schema_names(files[0])
    tgt = "target" if "target" in names else None
    wk = "WEEK_NUM" if "WEEK_NUM" in names else None
    use = ["case_id"] + [c for c in (tgt, wk) if c]
    parts = [pd.read_parquet(fp, columns=use) for fp in files]
    base = pd.concat(parts, ignore_index=True)
    base["case_id"] = base["case_id"].astype("int64", errors="ignore")
    return base.set_index("case_id")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    path = sys.argv[1]
    max_files = int(sys.argv[2]) if len(sys.argv) > 2 else 999

    files_by_stem = group_files(path)
    print("tables found:", {k: len(v) for k, v in files_by_stem.items()})

    inspect_schema(files_by_stem)

    print("\n" + "=" * 78)
    print("PART 2  --  raw per-case aggregates vs REAL target (univariate Gini)")
    print("=" * 78)
    base = load_base(files_by_stem)
    if base is None or "target" not in base.columns:
        print("no target column in base -> cannot score. (Part 1 still valid.)")
        return

    print("aggregating bureau DPD/overdue straight from raw rows...")
    bureau = aggregate_bureau(files_by_stem, max_files)
    print("aggregating applprev annuity...")
    ann = aggregate_annuity(files_by_stem, max_files)

    feats = base.copy()
    for frame in (bureau, ann):
        if not frame.empty:
            feats = feats.join(frame, how="left")

    feat_cols = [c for c in feats.columns if c not in ("target", "WEEK_NUM")]
    y = feats["target"].to_numpy(dtype=float)
    print(f"\nrows={len(feats):,}  default={np.nanmean(y):.3%}")
    print(f"bureau coverage: {int(feats['max_dpd'].notna().sum()) if 'max_dpd' in feats else 0:,} "
          f"cases have a bureau record\n")

    print(f"{'feature':22s} {'coverage%':>9s} {'nonzero':>9s} {'min':>8s} "
          f"{'max':>10s} {'mean':>10s} {'|Gini|':>8s}")
    rows = []
    for c in feat_cols:
        x = pd.to_numeric(feats[c], errors="coerce").to_numpy(dtype=float)
        cov = 100.0 * np.mean(~np.isnan(x))
        nz = int(np.nansum(x != 0))
        xf = x[~np.isnan(x)]
        mn = float(xf.min()) if xf.size else float("nan")
        mx = float(xf.max()) if xf.size else float("nan")
        me = float(xf.mean()) if xf.size else float("nan")
        # score with NaN -> 0 (a missing bureau record = no known delinquency)
        g = abs(gini(y, np.nan_to_num(x, nan=0.0)))
        rows.append((g, c, cov, nz, mn, mx, me))
    for g, c, cov, nz, mn, mx, me in sorted(rows, reverse=True):
        print(f"{c:22s} {cov:8.1f}% {nz:9d} {mn:8.1f} {mx:10.1f} {me:10.2f} {g:8.4f}")

    print("\nINTERPRETATION:")
    print("  * |Gini| > ~0.05 on a single raw column = real, usable signal.")
    print("  * Whichever columns light up here are the ones the adapter must read.")


if __name__ == "__main__":
    main()
