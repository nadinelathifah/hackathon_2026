"""Bridge between the Ibex scoring pipeline and the Polygon audit contract.

Drop this in as serve/score_event.py

It does three things:

1. Turns a scored applicant into the score-event.json shape the smart-contract
   repo expects (userId, newScore, timestamp, modelVersion, scoreBand,
   confidence, positiveFactors, negativeFactors).

2. Computes confidence properly, from the calibrator's own evidence, using a
   Beta-binomial posterior. This field is the one place in the demo where the
   Bayesian work becomes visible, so it should be real rather than a
   hard-coded 0.82.

3. Exposes a FastAPI router so the dashboard can produce the file on a button
   press.

Wiring into serve/app.py -- two lines:

    from serve.score_event import router as score_event_router
    app.include_router(score_event_router)


What confidence means here
--------------------------
The contract guide's example carries confidence 0.82 with no stated definition.
An unexplained number on a public blockchain is worse than no number, so this
module gives it one:

    confidence = P(the applicant's true PD falls inside the reported band
                   | the default counts observed near their score)

That is a posterior probability, computed as follows.

The calibrator stores, for each knot, n (observations) and k (defaults). At
roughly 1.9 observations per knot a single knot tells you almost nothing, so we
pool neighbouring knots around the applicant's score until we have at least
min_obs observations. That pooled (n, k) is the local evidence.

We place a Beta prior centred on the portfolio base rate with strength m,
measured in observations:

    prior = Beta(m * base_rate, m * (1 - base_rate))
    post  = Beta(k + m * base_rate, n - k + m * (1 - base_rate))

Conjugate, so the posterior is exact and closed form. m controls how hard the
base rate pulls a sparse local estimate back towards the portfolio mean. m=0 is
pure maximum likelihood, which returns PD=0 for a zero-default block -- the
pathology that produced the 895 ceiling. m=100 is what the sensitivity table
settled on. m=500 is heavily conservative.

The reported band maps to a score interval, which maps to a PD interval through
the scorecard. Integrating the posterior over that interval gives the
probability the band is right. A thin file in a sparse region of the score
distribution gets low confidence; a dense, well-evidenced region gets high
confidence. Nothing is hard-coded.

The same posterior yields the 95 percent upper PD, which is what a reported
ceiling should be pinned to. It is exposed as pd_upper95 and score_lower95 so
the frontend can show an honest interval instead of false precision.
"""

from __future__ import annotations

import json
import math
import os
import pickle
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------- scorecard
# Must match obcredit/modeling/scorecard.py
PDO = 40.0
BASE_SCORE = 600.0
BASE_ODDS = 20.0
FACTOR = PDO / math.log(2.0)
OFFSET = BASE_SCORE - FACTOR * math.log(BASE_ODDS)

NEG_INF = float("-inf")
POS_INF = float("inf")

BANDS: List[Tuple[float, str]] = [
    (720.0, "A"),
    (660.0, "B"),
    (600.0, "C"),
    (540.0, "D"),
    (NEG_INF, "E"),
]

MODEL_VERSION = os.environ.get("IBEX_MODEL_VERSION", "ibex-credit-model-v1.0")
PRIOR_STRENGTH = float(os.environ.get("IBEX_PRIOR_STRENGTH", "100"))
MIN_POOL_OBS = int(os.environ.get("IBEX_MIN_POOL_OBS", "200"))


def score_of_pd(pd: float) -> float:
    pd = min(max(pd, 1e-12), 1.0 - 1e-12)
    return OFFSET + FACTOR * math.log((1.0 - pd) / pd)


def pd_of_score(score: float) -> float:
    return 1.0 / (1.0 + math.exp((score - OFFSET) / FACTOR))


def band_of_score(score: float) -> str:
    for cutoff, name in BANDS:
        if score >= cutoff:
            return name
    return "E"


def band_score_bounds(band: str) -> Tuple[float, float]:
    upper = POS_INF
    for cutoff, name in BANDS:
        if name == band:
            return cutoff, upper
        upper = cutoff
    return NEG_INF, POS_INF


# ------------------------------------------- regularised incomplete beta
# Pure Python so this module has no scipy dependency and stays importable in
# the test environment. Lentz continued fraction, standard recipe.
def _betacf(a: float, b: float, x: float, itmax: int = 300,
            eps: float = 3e-16) -> float:
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta: P(Beta(a, b) <= x)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log1p(-x))
    front = math.exp(lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def beta_ppf(a: float, b: float, q: float, tol: float = 1e-10) -> float:
    """Quantile of Beta(a, b) by bisection. Dependency free and adequate."""
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if betainc(a, b, mid) < q:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)


# ------------------------------------------------------------ local evidence
def pool_local_evidence(x: Sequence[float], n: Sequence[float],
                        k: Sequence[float], score: float,
                        min_obs: int = MIN_POOL_OBS) -> Tuple[float, float, int]:
    """Pool knots either side of score until at least min_obs observations.

    The calibrator carries about 1.9 observations per knot, so a single knot is
    statistically empty. Returns (n_pooled, k_pooled, knots_used).
    """
    size = len(x)
    if size == 0:
        return 0.0, 0.0, 0

    lo, hi = 0, size - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if float(x[mid]) < score:
            lo = mid + 1
        else:
            hi = mid
    i = lo

    left = right = i
    n_tot = float(n[i])
    k_tot = float(k[i])
    while n_tot < min_obs and (left > 0 or right < size - 1):
        moved = False
        if left > 0:
            left -= 1
            n_tot += float(n[left])
            k_tot += float(k[left])
            moved = True
        if right < size - 1 and n_tot < min_obs:
            right += 1
            n_tot += float(n[right])
            k_tot += float(k[right])
            moved = True
        if not moved:
            break
    return n_tot, k_tot, right - left + 1


def posterior(n_obs: float, k_obs: float, base_rate: float,
              m: float = PRIOR_STRENGTH) -> Dict[str, float]:
    """Beta-binomial posterior over PD given local counts."""
    base_rate = min(max(base_rate, 1e-6), 1.0 - 1e-6)
    a = k_obs + m * base_rate
    b = (n_obs - k_obs) + m * (1.0 - base_rate)
    a = max(a, 1e-9)
    b = max(b, 1e-9)
    return {
        "alpha": a,
        "beta": b,
        "mean": a / (a + b),
        "lower05": beta_ppf(a, b, 0.05),
        "upper95": beta_ppf(a, b, 0.95),
        "n_obs": n_obs,
        "k_obs": k_obs,
        "prior_strength": m,
        "base_rate": base_rate,
    }


def band_confidence(post: Dict[str, float], band: str) -> float:
    """P(true PD lies inside the reported band's PD interval)."""
    lo_score, hi_score = band_score_bounds(band)
    # Higher score means lower PD, so the mapping inverts the interval.
    pd_hi = pd_of_score(lo_score) if lo_score > NEG_INF else 1.0
    pd_lo = pd_of_score(hi_score) if hi_score < POS_INF else 0.0
    a, b = post["alpha"], post["beta"]
    return max(0.0, min(1.0, betainc(a, b, pd_hi) - betainc(a, b, pd_lo)))


# ------------------------------------------------------------- calibrator io
_CALIB_CACHE: Dict[str, dict] = {}


def load_calibrator(path: Optional[str] = None) -> dict:
    path = path or os.environ.get(
        "IBEX_CALIBRATOR", os.path.join("artifacts", "calibrator.pkl"))
    if path in _CALIB_CACHE:
        return _CALIB_CACHE[path]
    with open(path, "rb") as fh:
        d = pickle.load(fh)
    _CALIB_CACHE[path] = d
    return d


def evidence_for_score(score: float, calib: Optional[dict] = None,
                       m: float = PRIOR_STRENGTH,
                       min_obs: int = MIN_POOL_OBS) -> Dict[str, object]:
    """Full Bayesian summary for one applicant score."""
    calib = calib if calib is not None else load_calibrator()
    x = calib.get("x")
    n = calib.get("n")
    k = calib.get("k")
    if x is None or n is None or k is None:
        # BUILD <= 17 pickles carry no counts; degrade honestly.
        return {"available": False,
                "reason": "calibrator has no n/k counts (pre-BUILD 18)"}

    n_tot = float(sum(float(v) for v in n))
    k_tot = float(sum(float(v) for v in k))
    base_rate = (k_tot / n_tot) if n_tot > 0 else 0.03871

    n_loc, k_loc, knots = pool_local_evidence(x, n, k, score, min_obs=min_obs)
    post = posterior(n_loc, k_loc, base_rate, m=m)
    band = band_of_score(score)

    return {
        "available": True,
        "score": score,
        "band": band,
        "knots_pooled": knots,
        "n_obs": post["n_obs"],
        "k_obs": post["k_obs"],
        "prior_strength": post["prior_strength"],
        "portfolio_base_rate": base_rate,
        "pd_posterior_mean": post["mean"],
        "pd_lower05": post["lower05"],
        "pd_upper95": post["upper95"],
        "score_upper95": score_of_pd(post["lower05"]),
        "score_lower95": score_of_pd(post["upper95"]),
        "confidence": band_confidence(post, band),
    }


# --------------------------------------------------------------- event build
def build_score_event(user_id: str, new_score: float,
                      pd: Optional[float] = None,
                      previous_score: Optional[float] = None,
                      positive_factors: Optional[List[str]] = None,
                      negative_factors: Optional[List[str]] = None,
                      calib: Optional[dict] = None,
                      model_version: str = MODEL_VERSION,
                      m: float = PRIOR_STRENGTH) -> Dict[str, object]:
    """Assemble the contract-ready event. No PII, no raw features.

    Deliberately excluded, per the contract guide's privacy list: names,
    addresses, bank details, transactions, income figures, raw ML features.
    userId should already be a pseudonymous handle; it is hashed with a private
    salt on the Node side before anything reaches the chain.
    """
    band = band_of_score(new_score)
    ev = evidence_for_score(new_score, calib=calib, m=m)

    event: Dict[str, object] = {
        "userId": user_id,
        "newScore": int(round(new_score)),
        "scoreBand": band,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "modelVersion": model_version,
        "positiveFactors": list(positive_factors or []),
        "negativeFactors": list(negative_factors or []),
    }
    if previous_score is not None:
        event["previousScore"] = int(round(previous_score))

    if ev.get("available"):
        event["confidence"] = round(float(ev["confidence"]), 4)
        event["scoreInterval95"] = [int(round(float(ev["score_lower95"]))),
                                    int(round(float(ev["score_upper95"])))]
        event["evidence"] = {
            "observations": int(float(ev["n_obs"])),
            "defaults": int(float(ev["k_obs"])),
            "priorStrength": int(float(ev["prior_strength"])),
            "method": "beta-binomial posterior; band membership probability",
        }
    else:
        event["confidence"] = None
        event["evidence"] = {"method": str(ev.get("reason", "unavailable"))}

    return event


def write_score_event(event: Dict[str, object], path: str) -> str:
    folder = os.path.dirname(os.path.abspath(path))
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(event, fh, indent=2)
    return os.path.abspath(path)


# ------------------------------------------------------------------- router
try:
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel, Field

    class ScoreEventRequest(BaseModel):
        user_id: str = Field(..., min_length=1)
        score: float
        pd: Optional[float] = None
        previous_score: Optional[float] = None
        positive_factors: List[str] = []
        negative_factors: List[str] = []
        out_path: Optional[str] = None
        prior_strength: Optional[float] = None

    router = APIRouter()

    @router.post("/api/score-event")
    def api_score_event(req: ScoreEventRequest) -> dict:
        """Build score-event.json for the Polygon audit contract."""
        try:
            calib = load_calibrator()
        except Exception as exc:
            raise HTTPException(status_code=500,
                                detail="could not load calibrator: %s" % exc)
        event = build_score_event(
            user_id=req.user_id,
            new_score=req.score,
            pd=req.pd,
            previous_score=req.previous_score,
            positive_factors=req.positive_factors,
            negative_factors=req.negative_factors,
            calib=calib,
            m=req.prior_strength or PRIOR_STRENGTH,
        )
        out_path = req.out_path or os.environ.get(
            "IBEX_SCORE_EVENT_PATH", "score-event.json")
        return {"event": event, "path": write_score_event(event, out_path)}

    @router.get("/api/score-evidence")
    def api_score_evidence(score: float,
                           prior_strength: float = PRIOR_STRENGTH) -> dict:
        """Bayesian evidence summary for a score, writing nothing."""
        try:
            calib = load_calibrator()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        return evidence_for_score(score, calib=calib, m=prior_strength)

except ImportError:  # fastapi absent (tests, offline): module still usable
    router = None  # type: ignore


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Write score-event.json")
    ap.add_argument("--user-id", required=True)
    ap.add_argument("--score", type=float, required=True)
    ap.add_argument("--previous-score", type=float, default=None)
    ap.add_argument("--out", default="score-event.json")
    ap.add_argument("--calibrator", default=None)
    ap.add_argument("--prior-strength", type=float, default=PRIOR_STRENGTH)
    ap.add_argument("--positive", action="append", default=[])
    ap.add_argument("--negative", action="append", default=[])
    a = ap.parse_args()

    calib_obj = load_calibrator(a.calibrator)
    built = build_score_event(
        user_id=a.user_id, new_score=a.score,
        previous_score=a.previous_score,
        positive_factors=a.positive, negative_factors=a.negative,
        calib=calib_obj, m=a.prior_strength)
    written_to = write_score_event(built, a.out)
    print(json.dumps(built, indent=2))
    print("\nwritten: %s" % written_to)
