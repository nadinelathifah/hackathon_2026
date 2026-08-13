"""BUILD 20 -- derived missingness / detection flags.

GOES IN: obcredit/missingness.py

ONE function, used at TRAINING and at SERVING time. Same parity discipline as
obcredit/feature_functions.py: if the two paths ever compute these differently,
the model is being served features it was not trained on.

These are deliberately NOT new @feature functions. A registry feature is computed
from the canonical transaction stream, so adding one means rebuilding the
1.5M-row OB matrix from Kaggle (a day of compute). These are pure functions of
feature values ALREADY in the matrix, so they cost seconds and are exactly as
reproducible.

WHY THEY EXIST
--------------
The shipped pipeline median-fills a missing feature (run_compare._prep_lgbm:
fillna(median).fillna(0.0)). For an applicant whose salary we could not detect,
that tells the model "this person earns the median". That is not a neutral
assumption, it is a false one, and it is wrong in the direction that matters:
no detectable regular income is itself a risk signal, and the median fill
erases it.

These flags let the trees split on "we could not measure this" directly, instead
of having to infer it from an imputed value that looks perfectly ordinary.
"""
from __future__ import annotations
from typing import Dict, List, Sequence

import numpy as np
import pandas as pd

# name -> monotone direction (+1 risk-increasing, -1 protective, 0 unconstrained)
# Asserted on credit reasoning, not fitted from data, exactly as in the registry.
MISSINGNESS_MONOTONE: Dict[str, int] = {
    "income_detected": -1,      # a detectable regular income is protective
    "thin_file": +1,            # no credit obligations to observe = more risk
    "declared_provided": 0,     # supplying declared attributes is not itself risk
    "n_features_missing": +1,   # the less we can measure, the worse
}

MISSINGNESS_NAMES: List[str] = list(MISSINGNESS_MONOTONE.keys())


def _num(frame: pd.DataFrame, col: str):
    if col not in frame.columns:
        return None
    return pd.to_numeric(frame[col], errors="coerce")


def add_missingness(frame: pd.DataFrame, base_cols: Sequence[str],
                    copy: bool = True) -> pd.DataFrame:
    """Add the missingness flags to `frame` and return it.

    base_cols: the model feature list WITHOUT these flags. Used for the row-wise
               missing count -- passing the flags in would make
               n_features_missing depend on itself.
    """
    out = frame.copy() if copy else frame
    n_rows = len(out)
    base = [c for c in base_cols if c in out.columns and c not in MISSINGNESS_NAMES]

    # 1) income_detected -- did the income detector fire at all?
    inc = _num(out, "monthly_income")
    if inc is None:
        out["income_detected"] = np.zeros(n_rows, dtype=float)
    else:
        out["income_detected"] = ((inc.notna()) & (inc > 0.0)).astype(float)

    # 2) thin_file -- no observable credit obligations on this connection
    obl = _num(out, "num_active_obligations")
    if obl is None:
        out["thin_file"] = np.ones(n_rows, dtype=float)
    else:
        out["thin_file"] = ((obl.isna()) | (obl <= 0.0)).astype(float)

    # 3) declared_provided -- did the applicant declare anything usable?
    gap = _num(out, "declared_income_gap")
    home = _num(out, "declared_is_homeowner")
    if gap is None and home is None:
        out["declared_provided"] = np.zeros(n_rows, dtype=float)
    else:
        ok = np.zeros(n_rows, dtype=bool)
        if gap is not None:
            ok = ok | gap.notna().to_numpy()
        if home is not None:
            ok = ok | home.notna().to_numpy()
        out["declared_provided"] = ok.astype(float)

    # 4) n_features_missing -- how much of the vector we could not measure
    if base:
        miss = out[base].apply(pd.to_numeric, errors="coerce").isna().sum(axis=1)
        out["n_features_missing"] = miss.astype(float)
    else:
        out["n_features_missing"] = np.zeros(n_rows, dtype=float)

    return out


def coverage_report(frame: pd.DataFrame) -> Dict[str, float]:
    """Mean of each flag, for the training log. A constant flag carries no
    information and should be dropped rather than shipped."""
    rep = {}
    for c in MISSINGNESS_NAMES:
        if c in frame.columns:
            v = pd.to_numeric(frame[c], errors="coerce")
            rep[c] = float(v.mean()) if len(v) else 0.0
    return rep
