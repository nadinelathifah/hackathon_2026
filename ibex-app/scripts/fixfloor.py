"""Set an evidence-based PD floor on a fitted calibrator.

Why this exists
---------------
The calibrator shipped with BASEL_PD_FLOOR = 0.0003 (3 basis points), the CRR
Article 160/163 regulatory minimum. That is a *floor on what you are allowed to
report*, not a claim about what your data can support. Ours could not support
it anywhere near.

The isotonic fit produced a zero-default block at the bottom of the score range
spanning 161 knots and 175 observations. Zero defaults in 175 observations does
not mean PD = 0; it means PD is small and poorly determined. The rule of three
gives a 95% upper bound of 3/175 = 0.0171. To justify a 0.0003 floor you would
need roughly 10,000 clean observations in that block. We have 175.

With pd_floor = 0.0003 the top of the scale reads 895. That number is produced
entirely by a constant, and it is outside every cell of the Bayesian table:

    m        posterior mean -> score      95% upper -> score
    20       0.00397 -> 746.0             0.01301 -> 677.0
    50       0.00860 -> 701.1             0.02054 -> 650.1
    100      0.01407 -> 672.3             0.02739 -> 633.1
    200      0.02064 -> 649.8             0.03398 -> 620.3
    500      0.02867 -> 630.4             0.03996 -> 610.6
    rule of three                         0.01714 -> 660.8

Default here is 0.0141, the m=100 posterior mean, giving a ceiling near 672.
Use 0.0274 (the m=100 95% upper bound, ceiling 633) if you want the
conservative Basel margin-of-conservatism reading instead.

This only changes the floor. Ranking, Gini and the backbone are untouched --
the floor binds at the very top of the score range and nowhere else, so Brier
and ECE move negligibly.

Usage
-----
    python fixfloor.py
    python fixfloor.py --floor 0.0274
    python fixfloor.py --path C:\\path\\to\\calibrator.pkl --dry-run
"""

from __future__ import annotations

import argparse
import math
import os
import pickle
import shutil
import sys

DEFAULT_PATH = r"C:\Users\Josep\Downloads\step3_build18\step3\artifacts\calibrator.pkl"
DEFAULT_FLOOR = 0.0141

# Scorecard constants -- must match obcredit/modeling/scorecard.py
PDO = 40.0
BASE_SCORE = 600.0
BASE_ODDS = 20.0
FACTOR = PDO / math.log(2.0)                     # 57.708
OFFSET = BASE_SCORE - FACTOR * math.log(BASE_ODDS)  # 427.13


def score_of(pd: float) -> float:
    return OFFSET + FACTOR * math.log((1.0 - pd) / pd)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=DEFAULT_PATH, help="calibrator.pkl location")
    ap.add_argument("--floor", type=float, default=DEFAULT_FLOOR,
                    help="new pd_floor (default %(default)s)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip writing calibrator.pkl.bak")
    args = ap.parse_args()

    if not os.path.exists(args.path):
        print("ERROR: not found: %s" % args.path)
        print("Pass the right location with --path")
        return 1

    if not (0.0 < args.floor < 1.0):
        print("ERROR: floor must be strictly between 0 and 1")
        return 1

    with open(args.path, "rb") as fh:
        d = pickle.load(fh)

    if not isinstance(d, dict) or "pd_floor" not in d:
        print("ERROR: this does not look like a calibrator pickle "
              "(no 'pd_floor' key). Keys: %s"
              % (sorted(d)[:12] if isinstance(d, dict) else type(d)))
        return 1

    old = float(d["pd_floor"])
    new = float(args.floor)

    print("file          : %s" % args.path)
    print("tail mode     : %s" % d.get("tail", "?"))
    print("old pd_floor  : %.6f   -> max score %.1f" % (old, score_of(old)))
    print("new pd_floor  : %.6f   -> max score %.1f" % (new, score_of(new)))

    # Report the evidence behind the change where the pickle carries it.
    n = d.get("n")
    k = d.get("k")
    if n is not None and k is not None:
        try:
            import numpy as np
            n = np.asarray(n, dtype=float)
            k = np.asarray(k, dtype=float)
            cum_k = np.cumsum(k)
            i = int(np.argmax(cum_k > 0)) if cum_k[-1] > 0 else len(k)
            n0 = float(np.cumsum(n)[i - 1]) if i > 0 else 0.0
            print("zero-default block: %d knots, %.0f observations" % (i, n0))
            if n0 > 0:
                r3 = 3.0 / n0
                print("rule-of-three 95%% upper PD: %.6f -> max score %.1f"
                      % (r3, score_of(r3)))
            if new > 0:
                print("observations needed to justify %.6f: %.0f"
                      % (old, 3.0 / old))
        except Exception as exc:  # pragma: no cover
            print("(evidence summary unavailable: %s)" % exc)

    if abs(old - new) < 1e-12:
        print("\nfloor already at target; nothing to do")
        return 0

    if args.dry_run:
        print("\nDRY RUN -- nothing written")
        return 0

    if not args.no_backup:
        bak = args.path + ".bak"
        shutil.copy2(args.path, bak)
        print("backup        : %s" % bak)

    d["pd_floor"] = new
    with open(args.path, "wb") as fh:
        pickle.dump(d, fh)

    with open(args.path, "rb") as fh:
        check = pickle.load(fh)
    got = float(check["pd_floor"])
    if abs(got - new) > 1e-12:
        print("ERROR: readback mismatch (%.6f)" % got)
        return 1

    print("\nOK -- pd_floor is now %.6f, top of scale about %.0f"
          % (got, score_of(got)))
    print("Band A (720+) is unreachable at this ceiling; say so explicitly "
          "rather than leaving an empty band in the writeup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
