"""Defensible scorecard: calibrated PD -> points via the industry-standard PDO
(points-to-double-the-odds) linear-in-log-odds transform, plus risk bands and
simple adverse-action reason codes. Transparent and auditable.

    score  = offset + factor * ln(odds_good)      odds_good = (1 - PD) / PD
    factor = PDO / ln(2)
    offset = base_score - factor * ln(base_odds)

Higher score = lower risk. With the defaults, 600 points == 20:1 good:bad odds
and every +40 points doubles the odds of being good.
"""
from __future__ import annotations
import math
from typing import List, Sequence, Tuple

import numpy as np

DEFAULT_PDO = 40.0
DEFAULT_BASE_SCORE = 600.0
DEFAULT_BASE_ODDS = 20.0

# (min_score_inclusive, label), highest band first. -inf is the open bottom.
BANDS: List[Tuple[float, str]] = [
    (720.0, "A"),
    (660.0, "B"),
    (600.0, "C"),
    (540.0, "D"),
    (float("-inf"), "E"),
]

_EPS = 1e-9


def pd_to_score(pd_value, pdo: float = DEFAULT_PDO,
                base_score: float = DEFAULT_BASE_SCORE,
                base_odds: float = DEFAULT_BASE_ODDS):
    """Convert a probability of default (scalar or array) to a credit score."""
    factor = pdo / math.log(2.0)
    offset = base_score - factor * math.log(base_odds)

    def _one(pd_v: float) -> float:
        pd_v = min(max(float(pd_v), _EPS), 1.0 - _EPS)
        odds_good = (1.0 - pd_v) / pd_v
        return float(offset + factor * math.log(odds_good))

    if np.isscalar(pd_value):
        return _one(pd_value)
    arr = np.asarray(pd_value, dtype=float)
    return np.array([_one(v) for v in arr], dtype=float)


def score_to_band(score):
    """Map a score (scalar or array) to a risk-band label using BANDS."""
    def _one(s: float) -> str:
        for cutoff, label in BANDS:
            if s >= cutoff:
                return label
        return BANDS[-1][1]

    if np.isscalar(score):
        return _one(score)
    return [_one(float(s)) for s in np.asarray(score, dtype=float)]


def top_reason_codes(contributions: Sequence[float], feature_names: Sequence[str],
                     k: int = 3) -> List[Tuple[str, float]]:
    """Return the k features with the largest risk-INCREASING contribution.

    `contributions` are per-feature pushes on the PD / log-odds (e.g. SHAP on the
    PD side); the most risk-increasing are returned as adverse-action reasons.
    """
    pairs = list(zip(list(feature_names), [float(c) for c in contributions]))
    pairs.sort(key=lambda kv: kv[1], reverse=True)
    return [(name, val) for name, val in pairs[:k]]
