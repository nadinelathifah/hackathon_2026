#!/usr/bin/env python3
r"""Compute the Home Credit stability metric from an evidence_se vintages CSV.

GOES IN: scripts/stability_from_vintages.py  (or run from anywhere)

The stability metric (the competition's own definition):

    weekly gini g_t, t = 1..T (in week order)
    fit OLS: g_t ~ a + b*t
    stability = mean(g) + 88 * min(0, b) - 0.5 * std(residuals)

It pays for average performance, penalises a DETERIORATING trend heavily (the
88*slope term only bites when the slope is negative), and penalises
week-to-week noise. This is the metric answer to "will the model still rank
well in six months?"

USAGE:
    py -3.13 scripts\stability_from_vintages.py artifacts_v5\vintages.csv

The script auto-detects the gini column (name contains 'gini') and the week
column (name contains 'week' or 'vintage'; otherwise row order is used) and
prints every component so the number is fully auditable.
"""
from __future__ import annotations
import sys

import numpy as np
import pandas as pd


def main(path: str) -> int:
    v = pd.read_csv(path)
    cols = {c.lower(): c for c in v.columns}
    gcol = next((cols[c] for c in cols if "gini" in c), None)
    wcol = next((cols[c] for c in cols if "week" in c or "vintage" in c), None)
    if gcol is None:
        raise SystemExit(f"FATAL: no column containing 'gini' in {list(v.columns)}")
    v = v.dropna(subset=[gcol])
    if wcol is not None:
        v = v.sort_values(wcol, kind="mergesort")
    g = v[gcol].to_numpy(float)
    T = len(g)
    if T < 3:
        raise SystemExit("FATAL: need at least 3 vintages for a trend.")

    t = np.arange(T, dtype=float)
    b, a = np.polyfit(t, g, 1)           # slope b, intercept a
    resid = g - (a + b * t)
    std_resid = float(resid.std())       # population std (competition convention)

    stability = g.mean() + 88.0 * min(0.0, b) - 0.5 * std_resid

    print(f"file                 : {path}")
    print(f"gini column          : {gcol}   week column: {wcol or '(row order)'}")
    print(f"vintages T           : {T}")
    print(f"mean weekly gini     : {g.mean():0.4f}")
    print(f"slope per week       : {b:+0.5f}")
    print(f"std of residuals     : {std_resid:0.4f}")
    print(f"penalty terms        : trend {88.0 * min(0.0, b):+0.4f}   "
          f"noise {-0.5 * std_resid:+0.4f}")
    print(f"STABILITY            : {stability:0.4f}")
    print()
    print("reading: the trend penalty is 0 unless weekly gini is DECLINING;")
    print("the noise penalty grows with week-to-week scatter around the trend.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: stability_from_vintages.py <vintages.csv>")
    raise SystemExit(main(sys.argv[1]))
