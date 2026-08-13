#!/usr/bin/env python3
"""How many rows CAN we train on? Answer before spending hours rebuilding.

The BUILD 18 run trained on 305,332 rows. Home Credit has ~1.5M applicants.
This prints where the population narrows, so you know the realistic ceiling
before you start -- and so "as many rows as possible" becomes a number you can
defend in the write-up instead of an accident.

Three stages:
  1) base*.parquet          -> the labelled universe (target + week live here)
  2) adapter universe       -> case_ids the KaggleAdapter will actually stream
  3) cached OB matrix       -> what you already built

Usage:
  py -3.13 scripts/diagnose_rows.py "C:\\Users\\Josep\\Downloads\\homecredit" \\
        --cache-dir "C:\\Users\\Josep\\Downloads\\obcache"
"""
from __future__ import annotations
import argparse
import glob
import os
import pickle
import re
import sys

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, os.path.join(_ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _stem(fp):
    s = os.path.splitext(os.path.basename(fp))[0]
    s = re.sub(r"^(train|test)_", "", s)
    return re.sub(r"_\d+$", "", s)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kaggle_dir")
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()

    import pyarrow.parquet as pq

    print("\n=========== STAGE 1: the labelled universe (base*.parquet) ===========")
    base_files = [fp for fp in sorted(glob.glob(os.path.join(args.kaggle_dir, "*.parquet")))
                  if _stem(fp) == "base"]
    if not base_files:
        print("  NO base*.parquet FOUND.")
        print("  Labels and the applicant universe both come from this file.")
        print("  Without it the adapter falls back to bureau case_ids and most")
        print("  rows end up unlabelled. This is a download problem: re-fetch")
        print("  train_base.parquet from the competition data.")
        base_ids, n_lab = set(), 0
    else:
        parts = []
        for fp in base_files:
            names = pq.ParquetFile(fp).schema.names
            cols = [c for c in ("case_id", "WEEK_NUM", "target") if c in names]
            parts.append(pd.read_parquet(fp, columns=cols))
            print(f"  {os.path.basename(fp):40s} {len(parts[-1]):>10,} rows  cols={cols}")
        base = pd.concat(parts, ignore_index=True)
        base_ids = set(str(c) for c in base["case_id"])
        n_lab = int(base["target"].notna().sum()) if "target" in base else 0
        print(f"  TOTAL unique case_id : {len(base_ids):>10,}")
        print(f"  with a target label  : {n_lab:>10,}")
        if "target" in base:
            print(f"  base default rate    : {float(base['target'].mean()):0.5f}")

    print("\n=========== STAGE 2: what the adapter will stream ===========")
    try:
        from obcredit.adapters import KaggleAdapter
        ad = KaggleAdapter.from_parquet_dir(args.kaggle_dir, max_cases=None)
        n_dec = len(getattr(ad, "_decision", {}) or {})
        n_pay = len(getattr(ad, "_pay_by_case", {}) or {})
        print(f"  case_ids with a decision date : {n_dec:>10,}")
        print(f"  case_ids with payment rows    : {n_pay:>10,}")
        if n_dec == 0:
            print("  !! decision map EMPTY -> adapter falls back to bureau case_ids.")
            print("     Those mostly have no label, so training silently shrinks.")
            universe = n_pay
        else:
            universe = n_dec
            both = len(set(map(str, ad._decision.keys())) & base_ids) if base_ids else 0
            print(f"  overlap with labelled base    : {both:>10,}")
    except Exception as e:
        print(f"  could not open adapter: {e}")
        universe = 0

    print("\n=========== STAGE 3: what you have already built ===========")
    cached = 0
    if args.cache_dir:
        for pat in ("ob_matrix_*.pkl", "ob_shards/ob_shard_*.pkl"):
            for fp in sorted(glob.glob(os.path.join(args.cache_dir, pat))):
                try:
                    with open(fp, "rb") as f:
                        df = pickle.load(f)
                    lab = int(pd.to_numeric(df["target"], errors="coerce").notna().sum()) \
                        if "target" in df.columns else 0
                    cached = max(cached, len(df))
                    print(f"  {os.path.basename(fp):40s} {len(df):>10,} rows  "
                          f"labelled {lab:,}")
                except Exception as e:
                    print(f"  {os.path.basename(fp):40s} unreadable ({e})")
    if not cached:
        print("  (nothing cached yet)")

    print("\n=========== VERDICT ===========")
    print(f"  labelled universe (the real ceiling) : {n_lab:>10,}")
    print(f"  adapter will stream                  : {universe:>10,}")
    print(f"  largest matrix built so far          : {cached:>10,}")
    if n_lab and cached:
        print(f"  you are currently using {100.0 * cached / n_lab:0.1f}% of what is available")
    if n_lab >= 500000:
        print("\n  -> 500k+ is achievable. Run build_ob_full.py --resume.")
    elif n_lab:
        print(f"\n  -> {n_lab:,} is the honest ceiling. Quote THIS number, not 1.5M.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
