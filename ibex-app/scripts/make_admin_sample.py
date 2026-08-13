#!/usr/bin/env python3
r"""Create the small admin-demo OB matrix sample.

The full OB matrix (ob_matrix_full_all.pkl) is gigabytes and exists only on
the research machine -- it cannot ship to Render (512 MB). This script draws
100 rows from the evaluation slice (the same population the admin cohort
scores) and writes fixtures/ob_matrix_admin100.pkl, which IS committed.

Run once locally, from the repo root:

    py -3.13 scripts\make_admin_sample.py

Then commit fixtures/ob_matrix_admin100.pkl. The admin cohort falls back to
it automatically whenever the full matrix is absent.
"""
import os
import sys

import pandas as pd

FULL = os.environ.get(
    "IBEX_OB_MATRIX",
    r"C:\Users\Josep\Downloads\obcache_b22\ob_matrix_full_all.pkl")
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "fixtures", "ob_matrix_admin100.pkl")
N = 100
SEED = 42


def main() -> int:
    if not os.path.exists(FULL):
        raise SystemExit(f"full matrix not found: {FULL}")
    print(f"reading {FULL} (about a minute) ...")
    m = pd.read_pickle(FULL)
    sub = m.dropna(subset=["target", "__week__"]).sort_values(
        "__week__", kind="mergesort")
    ev = sub.iloc[int(len(sub) * 0.8):]          # evaluation slice, as served
    take = min(N, len(ev))
    sample = ev.sample(take, random_state=SEED).copy()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    sample.to_pickle(OUT)
    print(f"wrote {OUT}  ({take} rows, {os.path.getsize(OUT) / 1e6:.2f} MB)")
    print("commit this file; the admin cohort uses it automatically when the")
    print("full matrix is absent (e.g. on Render).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
