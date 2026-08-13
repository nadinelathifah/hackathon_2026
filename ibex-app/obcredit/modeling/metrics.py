"""Metrics: AUC / Gini and the Home Credit competition stability metric.

Pure NumPy so the quick proof needs no scikit-learn. roc_auc uses the
Mann-Whitney U identity (rank-based, tie-aware) and drops NaNs pairwise.
"""
from __future__ import annotations
from typing import Dict

import numpy as np


def roc_auc(y_true, y_score) -> float:
    y = np.asarray(y_true, dtype=float)
    s = np.asarray(y_score, dtype=float)
    m = ~(np.isnan(y) | np.isnan(s))
    y, s = y[m], s[m]
    n_pos = float((y == 1).sum())
    n_neg = float((y == 0).sum())
    if n_pos == 0 or n_neg == 0:
        return 0.5
    order = np.argsort(s, kind="mergesort")
    s_sorted = s[order]
    ranks_sorted = np.empty(len(s), dtype=float)
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and s_sorted[j + 1] == s_sorted[i]:
            j += 1
        avg = (i + 1 + j + 1) / 2.0   # average rank for ties (1-based)
        ranks_sorted[i:j + 1] = avg
        i = j + 1
    ranks = np.empty(len(s), dtype=float)
    ranks[order] = ranks_sorted
    sum_pos = ranks[y == 1].sum()
    auc = (sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
    return float(auc)


def gini(y_true, y_score) -> float:
    return 2.0 * roc_auc(y_true, y_score) - 1.0


def gini_stability(weeks, y_true, y_score, w_falling: float = 88.0,
                   w_res_std: float = 0.5) -> Dict[str, object]:
    """Home Credit stability metric:
        metric = mean(weekly_gini) + 88 * min(0, slope) - 0.5 * std(residuals)
    where slope/residuals come from an OLS fit of weekly Gini against week.
    Weeks with <10 rows or a single class are skipped.
    """
    weeks = np.asarray(weeks, dtype=float)
    y = np.asarray(y_true, dtype=float)
    s = np.asarray(y_score, dtype=float)
    uniq = np.unique(weeks[~np.isnan(weeks)])
    weekly, wk = [], []
    for w in uniq:
        mask = weeks == w
        if mask.sum() < 10:
            continue
        yy = y[mask]
        if len(np.unique(yy)) < 2:
            continue
        weekly.append(gini(yy, s[mask]))
        wk.append(w)
    weekly = np.asarray(weekly, dtype=float)
    wk = np.asarray(wk, dtype=float)
    if len(weekly) == 0:
        return {"metric": 0.0, "mean_gini": 0.0, "slope": 0.0,
                "res_std": 0.0, "weekly": []}
    if len(weekly) >= 2:
        A = np.vstack([wk, np.ones_like(wk)]).T
        slope, intercept = np.linalg.lstsq(A, weekly, rcond=None)[0]
        resid = weekly - (slope * wk + intercept)
        res_std = float(resid.std())
    else:
        slope, res_std = 0.0, 0.0
    mean_gini = float(weekly.mean())
    metric = mean_gini + w_falling * min(0.0, float(slope)) - w_res_std * res_std
    return {"metric": float(metric), "mean_gini": mean_gini,
            "slope": float(slope), "res_std": res_std,
            "weekly": list(zip(wk.tolist(), weekly.tolist()))}
