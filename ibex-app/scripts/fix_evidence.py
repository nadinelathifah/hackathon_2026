#!/usr/bin/env python3
r"""
fix_evidence.py -- stop /api/v4/evidence serving another build's numbers.

The bug: evidence() returned hardcoded BUILD-18 figures (Gini 0.4097,
block SE 0.0103, design effect 1.72x, tail 306 obs, a 759.5 score row)
and attached any real measured run under a "live" key that the card never
read. Floor and ceiling were computed live from the calibrator, so the
panel showed v5 floor/ceiling next to v3-era Gini and a score row ABOVE
the stated ceiling.

This patch makes evidence() read artifacts/evidence_se.json and map it
onto the fields the card renders, and return has_run=False with no
invented numbers when that file is absent.

Usage (from the step3 repo root):
    py -3.13 scripts\fix_evidence.py
Idempotent: running twice is a no-op.
"""
import os
import sys

TARGET = os.path.join("serve", "ibex_v4.py")
START = '@router.get("/api/v4/evidence")'
END = "# ======================================================================\n# 4. BUSINESS VERIFICATION"
MARK = "# [patched by fix_evidence.py]"

NEW = '''@router.get("/api/v4/evidence")
def evidence(ibex_session: Optional[str] = Cookie(None)):
    ''' + '"""' + '''
    ''' + MARK + '''
    Serves the measured uncertainty panel.

    Figures come from artifacts/evidence_se.json, written by
    scripts/evidence_se.py --json-out. If that file is missing we return
    has_run=False and NO figures, so the panel states that uncertainty
    has not been measured for the artifacts currently being served
    instead of showing a previous build's numbers.
    ''' + '"""' + '''
    _require(ibex_session)
    path = os.path.join(ARTIFACTS, "evidence_se.json")
    live = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                live = json.load(fh)
        except Exception:
            live = None

    floor = _floor_now()
    ceiling = None
    if floor and 0 < floor < 1:
        ceiling = round(OFFSET + FACTOR * math.log((1 - floor) / floor), 1)

    caveats = [
        "Percentile intervals. Never converted via +/- 1.96 SE -- the "
        "replicate distributions are materially asymmetric.",
        "The booster was held fixed across replicates, so intervals "
        "reflect calibration and evaluation sampling variability, not "
        "model-fitting variability.",
        "The upper bound at the ceiling score is censored by the PD floor "
        "and should be read as at or below the floor.",
        "Calibration and evaluation slices cover different origination "
        "weeks; the base-rate difference is right-censoring, not model "
        "error.",
    ]

    if live is None:
        return {
            "has_run": False,
            "stale": True,
            "floor": floor,
            "ceiling": ceiling,
            "replicates": 0,
            "method": "block bootstrap over origination weeks",
            "gini": None,
            "tail": None,
            "points": [],
            "caveats": caveats,
            "message": (
                "Uncertainty has not been measured for the artifacts being "
                "served (" + str(ARTIFACTS) + "). Run scripts/evidence_se.py "
                "with --json-out " + path + " to populate this panel."
            ),
        }

    def _pair(v):
        try:
            return [float(v[0]), float(v[1])]
        except Exception:
            return None

    def _num(v, default=None):
        try:
            return float(v)
        except Exception:
            return default

    scores = live.get("ref_scores") or []
    pds = live.get("ref_pd_point") or []
    pd_cis = live.get("ref_pd_ci") or []
    sc_cis = live.get("ref_score_ci") or []
    points = []
    for i, sc in enumerate(scores):
        if i >= len(pds):
            break
        pd_ci = _pair(pd_cis[i]) if i < len(pd_cis) else None
        sc_ci = _pair(sc_cis[i]) if i < len(sc_cis) else None
        if pd_ci is None or sc_ci is None:
            continue
        points.append({
            "score": round(float(sc), 1),
            "pd": float(pds[i]),
            "pd_ci": pd_ci,
            "score_ci": sc_ci,
        })

    gini_point = _num(live.get("gini_point"))
    if gini_point is None:
        return {
            "has_run": False,
            "stale": True,
            "floor": floor,
            "ceiling": ceiling,
            "replicates": 0,
            "method": "block bootstrap over origination weeks",
            "gini": None,
            "tail": None,
            "points": [],
            "caveats": caveats,
            "message": "evidence_se.json is present but unreadable; re-run "
                       "scripts/evidence_se.py --json-out " + path,
        }

    tail_ci = _pair(live.get("tail_n_ci")) or [0.0, 0.0]
    return {
        "has_run": True,
        "stale": False,
        "floor": floor,
        "ceiling": ceiling,
        "replicates": int(live.get("n_boot") or 0),
        "method": "block bootstrap over origination weeks",
        "n_eval": live.get("n_eval"),
        "n_weeks_eval": live.get("n_weeks_eval"),
        "gini": {
            "point": gini_point,
            "ci": _pair(live.get("gini_ci_block")) or [gini_point, gini_point],
            "se_block": _num(live.get("se_gini_block"), 0.0),
            "se_delong": _num(live.get("se_gini_delong"), 0.0),
            "design_effect": round(_num(live.get("design_effect"), 0.0), 2),
        },
        "tail": {
            "obs": int(_num(live.get("tail_n_point"), 0)),
            "defaults": int(_num(live.get("tail_k_point"), 0)),
            "ci": [int(round(tail_ci[0])), int(round(tail_ci[1]))],
            "rule_of_three": live.get("rule_of_three"),
            "floor_ci": _pair(live.get("floor_ci")),
        },
        "floor_measured": live.get("floor_point"),
        "mean_pd_ci": _pair(live.get("mean_pd_ci")),
        "caveats": caveats,
        "points": points,
    }


'''


def main():
    if not os.path.exists(TARGET):
        print("ERROR: run this from the step3 repo root (missing %s)" % TARGET)
        return 1
    src = open(TARGET, encoding="utf-8").read()
    if MARK in src:
        print("[fix_evidence] already patched -- nothing to do")
        return 0
    i = src.find(START)
    j = src.find(END)
    if i < 0 or j < 0 or j <= i:
        print("ERROR: could not locate the evidence() function; file may have "
              "been edited. No changes written.")
        return 1
    bak = TARGET + ".evidence.bak"
    if not os.path.exists(bak):
        open(bak, "w", encoding="utf-8").write(src)
        print("[fix_evidence] backup -> %s" % bak)
    out = src[:i] + NEW + src[j:]
    open(TARGET, "w", encoding="utf-8").write(out)
    print("[fix_evidence] patched %s" % TARGET)
    print("[fix_evidence] restart uvicorn, then reload /ibex")
    return 0


if __name__ == "__main__":
    sys.exit(main())
