#!/usr/bin/env python3
"""fix_model_page.py -- correct the misreported SE figures in model.html.

Run from the step3 repo root:
    py -3.13 scripts/fix_model_page.py --artifacts artifacts_v5

Reads the real numbers out of <artifacts>/evidence_se.json and
<artifacts>/vintages.csv and rewrites the hand-typed figures in
serve/static/model.html so the page cannot drift from the artifacts again.

Idempotent: a second run is a no-op. Backs up to model.html.sebak once.
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import sys

TARGET = os.path.join("serve", "static", "model.html")
MARK = "<!-- se-report-fixed -->"


def load(artifacts):
    p = os.path.join(artifacts, "evidence_se.json")
    if not os.path.exists(p):
        sys.exit("FATAL: no evidence_se.json in " + artifacts)
    with open(p, encoding="utf-8") as fh:
        ev = json.load(fh)
    rows = []
    vp = os.path.join(artifacts, "vintages.csv")
    if os.path.exists(vp):
        with open(vp, newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.DictReader(fh) if r.get("gini")]
    return ev, rows


def stability(rows):
    import numpy as np
    g = np.array([float(r["gini"]) for r in rows], dtype=float)
    t = np.arange(len(g), dtype=float)
    b, a = np.polyfit(t, g, 1)
    resid = g - (a + b * t)
    val = g.mean() + 88.0 * min(0.0, b) - 0.5 * float(resid.std())
    return val, float(b), float(g.mean()), float(resid.std())


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", default="artifacts_v5")
    a = ap.parse_args(argv)

    if not os.path.exists(TARGET):
        sys.exit("FATAL: run from the repo root (missing " + TARGET + ")")

    ev, rows = load(a.artifacts)
    html = open(TARGET, encoding="utf-8").read()
    if MARK in html:
        print("[fix_model_page] already patched -- nothing to do")
        return 0

    weeks = int(ev.get("n_weeks_eval") or 0)
    stab, slope, meang, sdres = stability(rows) if rows else (None, 0, 0, 0)
    first = float(rows[0]["default_rate"]) * 100 if rows else None
    last = float(rows[-1]["default_rate"]) * 100 if rows else None
    clip = (ev.get("floor_clipped_frac") or [0])[-1] * 100
    nbins = int(ev.get("nbins") or 0)

    reps = []

    # 1. the block count -- the page said 56, the bootstrap used n_weeks_eval
    reps.append(("fall into 56 weekly origination cohorts",
                 "fall into %d weekly origination cohorts" % weeks))
    reps.append(("Draw 56 blocks with replacement",
                 "Draw %d blocks with replacement" % weeks))

    # 2. Jeffreys is wrong -- the prior is informative, strength 100
    reps.append((
        "Jeffreys Beta posterior over the safest bucket;",
        "Beta posterior over the safest bucket using an informative prior "
        "of strength 100 centred on the portfolio base rate;"))

    # 3. the drift figures did not match vintages.csv
    if first is not None:
        reps.append(("drifts from 3.35% to 2.13% across",
                     "drifts from %.2f%% to %.2f%% across" % (first, last)))

    # 4. the stability slot never carried a value
    if stab is not None:
        reps.append((
            "It is recomputed at each retrain and reported with the model, "
            "not asserted once.",
            "It is recomputed at each retrain and reported with the model, "
            "not asserted once. On the current build it is %.4f: mean weekly "
            "Gini %.4f, slope %+.5f per week, so the deterioration penalty "
            "is zero and the whole gap is week-to-week scatter."
            % (stab, meang, slope)))

    out = html
    done, missed = [], []
    for old, new in reps:
        if old in out:
            out = out.replace(old, new)
            done.append(old[:52])
        else:
            missed.append(old[:52])

    # 5. the four disclosures, appended to the quantity table
    extra = (
        '<tr><td>Cluster count</td><td>The block bootstrap resamples %d '
        'origination weeks. Cluster-resampled inference is known to be '
        'unreliable below roughly 40 clusters, and biased downward, so the '
        'quoted standard error should be read as a lower bound.</td>'
        '<td class="n">%d weeks</td></tr>'
        '<tr><td>Calibrator used in the bootstrap</td><td>Replicates refit '
        'isotonic regression on %s quantile bins (~%d rows per knot) rather '
        'than production\u2019s fit on every distinct raw score. Faster and '
        'better behaved, but a slightly different estimand.</td>'
        '<td class="n">%s bins</td></tr>'
        '<tr><td>What is held fixed</td><td>The booster is not refitted '
        'across replicates, so these intervals capture calibration and '
        'evaluation sampling variability, and are conditional on this '
        'fitted model.</td><td class="n">booster fixed</td></tr>'
        '<tr><td>Censoring at the ceiling</td><td>At the top reference score '
        '%.1f%% of replicates clip at the probability floor, so the upper '
        'bound is censored and must be read as at or below the floor.</td>'
        '<td class="n">%.1f%% clipped</td></tr>'
    ) % (weeks, weeks, format(nbins, ","),
         round((ev.get("n_eval") or 0) / nbins) if nbins else 0,
         format(nbins, ","), clip, clip)

    anchor = ('<td class="n">%d obs, %d defaults</td></tr>'
              % (int(ev.get("tail_n_point") or 0),
                 int(ev.get("tail_k_point") or 0)))
    if anchor in out:
        out = out.replace(anchor, anchor + extra, 1)
        done.append("appended 4 disclosure rows")
    else:
        missed.append("disclosure rows anchor")

    bak = TARGET + ".sebak"
    if not os.path.exists(bak):
        open(bak, "w", encoding="utf-8").write(html)
    open(TARGET, "w", encoding="utf-8").write(out + MARK)

    for d in done:
        print("  fixed   " + d)
    for m in missed:
        print("  MISSING " + m + "   (string not found -- check by hand)")
    print("[fix_model_page] wrote %s (%d fixed, %d missing)"
          % (TARGET, len(done), len(missed)))
    return 1 if missed else 0


if __name__ == "__main__":
    raise SystemExit(main())
