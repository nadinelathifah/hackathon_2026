"""BUILD 19 -- per-score confidence intervals from the calibrator block structure.

Why this exists
---------------
The scorecard maps PD -> score.  PAVA groups applicants into blocks of equal
fitted PD, so every applicant inherits the PD of the block they land in.  That
block has a finite sample size (n) and a finite number of observed defaults (k),
so the PD it reports carries sampling error.  This module turns that error into
a score interval and a band range.

Two honest caveats, which must be reported alongside the numbers:

  1. The interval is CONDITIONAL on the block structure PAVA chose.  Re-running
     the calibration on a resample would move the block boundaries as well as
     the rates inside them.  These intervals therefore UNDERSTATE total
     uncertainty.  The block bootstrap in scripts/evidence_se.py is the
     unconditional counterpart.

  2. A block with zero observed defaults has NO upper bound from the data.  Its
     upper bound is the policy ceiling, which is a choice, not a measurement.
     Rows where that applies are flagged `upper_is_policy`.

Blocks that share an identical fitted PD are pooled before the interval is
computed.  Two blocks with the same PD are indistinguishable to the scorecard,
so reporting them separately would overstate how finely the data resolves them.
This matters after a floor is applied, because the floor deliberately ties the
bottom blocks together.
"""

from __future__ import annotations

import math
import os
import pickle
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

# Scorecard constants -- must match obcredit/modeling/scorecard.py
DEFAULT_PDO = 40.0
DEFAULT_BASE_SCORE = 600.0
DEFAULT_BASE_ODDS = 20.0
FACTOR = DEFAULT_PDO / math.log(2.0)          # 57.7078
OFFSET = DEFAULT_BASE_SCORE - FACTOR * math.log(DEFAULT_BASE_ODDS)   # 427.1229

BANDS: Sequence = ((720.0, "A"), (660.0, "B"), (600.0, "C"), (540.0, "D"))
Z95 = 1.959963984540054
_EPS = 1e-12


def score_of_pd(pd: float) -> float:
    """PD -> score.  Clamped so the log stays finite."""
    p = min(max(float(pd), 1e-9), 1.0 - 1e-9)
    return OFFSET + FACTOR * math.log((1.0 - p) / p)


def pd_of_score(score: float) -> float:
    """Score -> PD.  Inverse of score_of_pd."""
    z = (float(score) - OFFSET) / FACTOR
    return 1.0 / (1.0 + math.exp(z))


def band_of(score: float) -> str:
    for threshold, label in BANDS:
        if score >= threshold:
            return label
    return "E"


def band_range(score_lo: float, score_hi: float) -> str:
    """Band label, or 'X-Y' when the interval straddles a band boundary."""
    lo, hi = band_of(score_lo), band_of(score_hi)
    return lo if lo == hi else "%s-%s" % (lo, hi)


def wilson(k: float, n: float, z: float = Z95) -> tuple:
    """Wilson score interval for a binomial proportion.

    Chosen over the normal approximation because it stays inside [0, 1] and
    remains sensible at k = 0 and at small n, both of which occur in the tail.
    At k = 0 the lower bound is exactly 0, which is why zero-default blocks
    have no data-driven upper score bound.
    """
    n = float(n)
    if n <= 0:
        return 0.0, 1.0
    k = float(k)
    p = k / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2.0 * n)
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    lo = (centre - half) / denom
    hi = (centre + half) / denom
    return max(lo, 0.0), min(hi, 1.0)


class BlockTable:
    """The calibrator's PAVA blocks, with the PD floor applied and ties pooled."""

    def __init__(self, x, y, n, k, pd_floor: float):
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        n = np.asarray(n, dtype=float)
        k = np.asarray(k, dtype=float)
        self.pd_floor = float(pd_floor)

        # Apply the floor exactly as the runtime clip does, so what we report
        # matches what the applicant is actually scored on.
        y_eff = np.maximum(y, self.pd_floor)

        # Split into runs of equal fitted PD.  Blocks tied by the floor pool
        # together, which is the honest treatment -- the scorecard cannot tell
        # them apart.
        cuts = np.r_[0, np.nonzero(np.diff(y_eff) > _EPS)[0] + 1, len(y_eff)]
        self.blocks: List[Dict[str, Any]] = []
        for i in range(len(cuts) - 1):
            s, e = int(cuts[i]), int(cuts[i + 1])
            self.blocks.append({
                "index": i,
                "knots": e - s,
                "n": float(n[s:e].sum()),
                "k": float(k[s:e].sum()),
                "pd": float(y_eff[s]),
                "x_lo": float(x[s]),
                "x_hi": float(x[e - 1]),
            })

        # The ceiling is the score of the lowest reportable PD.
        self.ceiling_pd = float(min(b["pd"] for b in self.blocks)) if self.blocks else self.pd_floor
        self.ceiling_score = score_of_pd(self.ceiling_pd)

    # -- lookup ---------------------------------------------------------

    def block_for_pd(self, pd: float) -> Optional[Dict[str, Any]]:
        """Find the block whose fitted PD matches, else the nearest one."""
        if not self.blocks:
            return None
        pd = float(pd)
        for b in self.blocks:
            if abs(b["pd"] - pd) <= 1e-9:
                return b
        return min(self.blocks, key=lambda b: abs(b["pd"] - pd))

    def block_for_x(self, x_val: float) -> Optional[Dict[str, Any]]:
        """Find the block containing a raw model output.

        Only use this if the runtime `x` is on the same scale as the
        calibrator's stored `x`.  Verify before relying on it; otherwise use
        block_for_pd, which is scale-free.
        """
        if not self.blocks:
            return None
        x_val = float(x_val)
        for b in self.blocks:
            if b["x_lo"] - _EPS <= x_val <= b["x_hi"] + _EPS:
                return b
        return min(self.blocks,
                   key=lambda b: min(abs(b["x_lo"] - x_val), abs(b["x_hi"] - x_val)))

    # -- intervals ------------------------------------------------------

    def interval(self, block: Dict[str, Any], z: float = Z95) -> Dict[str, Any]:
        """Wilson interval on a block, expressed on the score scale."""
        n, k = block["n"], block["k"]
        pd_lo, pd_hi = wilson(k, n, z)

        # Higher PD -> lower score, so the bounds swap.
        score_lo = score_of_pd(min(max(pd_hi, 1e-9), 0.5))

        upper_is_policy = False
        if pd_lo <= 0.0:
            # No observed defaults: the data places no upper bound at all.
            score_hi = self.ceiling_score
            upper_is_policy = True
        else:
            raw_hi = score_of_pd(pd_lo)
            score_hi = min(raw_hi, self.ceiling_score)
            if raw_hi > self.ceiling_score + 1e-9:
                upper_is_policy = True

        score_lo = min(score_lo, self.ceiling_score)
        point = min(score_of_pd(block["pd"]), self.ceiling_score)

        return {
            "block": block["index"],
            "n": int(round(n)),
            "k": int(round(k)),
            "knots": block["knots"],
            "pd": block["pd"],
            "pd_lo": pd_lo,
            "pd_hi": pd_hi,
            "score": round(point, 1),
            "score_lo": round(score_lo, 1),
            "score_hi": round(score_hi, 1),
            "width": round(score_hi - score_lo, 1),
            "band": band_of(point),
            "band_range": band_range(score_lo, score_hi),
            "upper_is_policy": upper_is_policy,
            "confidence": 0.95,
        }

    def for_pd(self, pd: float) -> Optional[Dict[str, Any]]:
        b = self.block_for_pd(pd)
        return self.interval(b) if b else None

    def rows(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        blocks = self.blocks if limit is None else self.blocks[:limit]
        return [self.interval(b) for b in blocks]


# ---------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------

def _artifacts_dir() -> str:
    return os.environ.get("IBEX_ARTIFACTS", "artifacts")


def calibrator_path() -> str:
    explicit = os.environ.get("IBEX_CALIBRATOR")
    if explicit:
        return explicit
    return os.path.join(_artifacts_dir(), "calibrator.pkl")


_CACHE: Dict[str, Any] = {"path": None, "mtime": None, "table": None}


def load_table(path: Optional[str] = None, force: bool = False) -> BlockTable:
    """Load and cache the block table, invalidating on file mtime."""
    path = path or calibrator_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError as exc:
        raise FileNotFoundError("calibrator not found at %s" % path) from exc

    if (not force and _CACHE["table"] is not None
            and _CACHE["path"] == path and _CACHE["mtime"] == mtime):
        return _CACHE["table"]

    with open(path, "rb") as fh:
        d = pickle.load(fh)

    missing = [key for key in ("x", "y", "n", "k") if key not in d]
    if missing:
        raise KeyError("calibrator is missing keys: %s" % ", ".join(missing))

    floor = d.get("pd_floor")
    floor = 0.0003 if floor is None else float(floor)

    table = BlockTable(d["x"], d["y"], d["n"], d["k"], floor)
    _CACHE.update({"path": path, "mtime": mtime, "table": table})
    return table


# ---------------------------------------------------------------------
# FastAPI router  (import is lazy so the module stays usable without fastapi)
# ---------------------------------------------------------------------

def build_router():
    from fastapi import APIRouter, Query
    from fastapi.responses import JSONResponse

    router = APIRouter()

    @router.get("/api/v4/interval")
    def interval(pd: float = Query(..., gt=0.0, lt=1.0,
                                   description="Fitted PD from the score run")):
        """95% interval and band range for the block this PD falls in."""
        try:
            table = load_table()
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        row = table.for_pd(pd)
        if row is None:
            return JSONResponse({"error": "no blocks in calibrator"}, status_code=503)
        row["ceiling_score"] = round(table.ceiling_score, 1)
        row["pd_floor"] = table.pd_floor
        row["caveat"] = ("Interval is conditional on the calibration block "
                         "structure and understates total uncertainty.")
        return row

    @router.get("/api/v4/intervals/table")
    def intervals_table(limit: int = Query(25, ge=1, le=500)):
        """Block-by-block interval table -- the diagnostic view."""
        try:
            table = load_table()
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        return {
            "pd_floor": table.pd_floor,
            "ceiling_score": round(table.ceiling_score, 1),
            "blocks_total": len(table.blocks),
            "rows": table.rows(limit=limit),
            "caveats": [
                "Intervals are conditional on the PAVA block structure.",
                "upper_is_policy=true means the upper bound is the ceiling, not a measurement.",
                "Blocks sharing a fitted PD are pooled; they are indistinguishable to the scorecard.",
            ],
        }

    return router


# ---------------------------------------------------------------------
# CLI:  python -m serve.intervals [--limit N] [--pd 0.0045]
# ---------------------------------------------------------------------

def _print_table(table: BlockTable, limit: int) -> None:
    print("pd_floor %.8f   ceiling %.1f   blocks %d"
          % (table.pd_floor, table.ceiling_score, len(table.blocks)))
    print("=" * 78)
    print(" blk      n    k    score       95% interval      width   band range")
    for r in table.rows(limit=limit):
        flag = " *" if r["upper_is_policy"] else ""
        print("%4d %6d %4d   %6.1f     [%6.1f, %6.1f]  %6.1f   %s%s"
              % (r["block"], r["n"], r["k"], r["score"],
                 r["score_lo"], r["score_hi"], r["width"], r["band_range"], flag))
    print()
    print("  * upper bound is the policy ceiling, not a measurement")


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Per-block score intervals")
    ap.add_argument("--calibrator", default=None)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--pd", type=float, default=None,
                    help="Report the interval for a single PD instead")
    args = ap.parse_args(argv)

    table = load_table(args.calibrator)
    if args.pd is not None:
        row = table.for_pd(args.pd)
        print("pd %.8f -> block %d  n=%d k=%d" % (args.pd, row["block"], row["n"], row["k"]))
        print("score %.1f   95%% [%.1f, %.1f]   width %.1f   band %s"
              % (row["score"], row["score_lo"], row["score_hi"],
                 row["width"], row["band_range"]))
        if row["upper_is_policy"]:
            print("upper bound is the policy ceiling, not a measurement")
        return 0

    _print_table(table, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
