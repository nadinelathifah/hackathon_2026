"""Tests for scripts/run_logreg.py -- the standalone parity-safe LR pipeline.

The sandbox has no parquet engine, so we test the PURE aggregation + assembly +
model logic on in-memory pandas frames that mimic the real Home Credit tables.
The file-reading wrapper (build_features) is a thin loop around these same
functions and mirrors diagnose_dpd.py, which already runs on the real data.
"""
from __future__ import annotations
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import run_logreg as R  # noqa: E402


def _approx(a, b, tol=1e-6):
    assert abs(a - b) <= tol, f"{a} != {b}"


def test_bureau_chunk_basic():
    # case 1: dpd values 0, 10, 40 ; overdue 0, 5, 20
    # case 2: single clean payment
    df = pd.DataFrame({
        "case_id": [1, 1, 1, 2],
        "pmts_dpd_1073P": [0, 10, 40, 0],
        "pmts_overdue_1140A": [0, 5, 20, 0],
    })
    agg = R.agg_bureau_chunk(df)
    c1 = agg.loc[1]
    _approx(c1["dpd_max"], 40)
    _approx(c1["dpd_sum"], 50)
    _approx(c1["n_pmts"], 3)
    _approx(c1["n_dpd_gt0"], 2)
    _approx(c1["n_dpd_ge30"], 1)
    _approx(c1["ovd_max"], 20)
    _approx(c1["ovd_sum"], 25)


def test_bureau_dpd_capped_at_90():
    # b_2 has absurd raw outliers (e.g. 1.8e8). Must clip to 90.
    df = pd.DataFrame({
        "case_id": [7, 7],
        "pmts_dpdvalue_108P": [185124192, 120],
        "pmts_pmtsoverdue_635A": [10, 10],
    })
    agg = R.agg_bureau_chunk(df)
    _approx(agg.loc[7]["dpd_max"], 90)   # both capped
    _approx(agg.loc[7]["dpd_sum"], 180)  # 90 + 90


def test_bureau_text_garbage_coerced():
    df = pd.DataFrame({
        "case_id": [3, 3],
        "pmts_dpd_1073P": ["n/a", 15],
        "pmts_overdue_1140A": [None, 3],
    })
    agg = R.agg_bureau_chunk(df)
    _approx(agg.loc[3]["dpd_max"], 15)
    _approx(agg.loc[3]["n_dpd_gt0"], 1)


def test_combine_bureau_true_mean_across_chunks():
    # same case split across two chunks; mean must be sum/count, not mean-of-means
    df1 = pd.DataFrame({"case_id": [5, 5], "pmts_dpd_1073P": [0, 0],
                        "pmts_overdue_1140A": [0, 0]})
    df2 = pd.DataFrame({"case_id": [5], "pmts_dpd_1073P": [30],
                        "pmts_overdue_1140A": [9]})
    comb = R.combine_bureau([R.agg_bureau_chunk(df1), R.agg_bureau_chunk(df2)])
    row = comb.loc[5]
    _approx(row["max_dpd"], 30)
    _approx(row["num_bureau_payments"], 3)
    _approx(row["mean_dpd"], 10)          # 30 / 3, NOT (0 + 30)/2
    _approx(row["num_dpd_gt0"], 1)
    _approx(row["total_overdue"], 9)


def test_applprev_aggregation():
    df = pd.DataFrame({"case_id": [1, 1, 2], "annuity_853A": [100, 200, 50]})
    comb = R.combine_applprev([R.agg_applprev_chunk(df)])
    _approx(comb.loc[1]["total_annuity"], 300)
    _approx(comb.loc[1]["max_annuity"], 200)
    _approx(comb.loc[1]["num_prev_apps"], 2)
    _approx(comb.loc[2]["total_annuity"], 50)


def test_assemble_fills_and_dti():
    base = pd.DataFrame({"case_id": [1, 2, 3], "target": [1, 0, 0],
                         "WEEK_NUM": [1, 1, 2]}).set_index("case_id")
    bureau = R.combine_bureau([R.agg_bureau_chunk(pd.DataFrame({
        "case_id": [1, 1], "pmts_dpd_1073P": [40, 0],
        "pmts_overdue_1140A": [10, 0]}))])
    applprev = R.combine_applprev([R.agg_applprev_chunk(pd.DataFrame({
        "case_id": [1], "annuity_853A": [200]}))])
    income = pd.DataFrame({"monthly_income": [1000.0]}, index=pd.Index([1], name="case_id"))
    feats = R.assemble_features(base, bureau, applprev, income)
    # case 1 has records
    _approx(feats.loc[1]["max_dpd"], 40)
    _approx(feats.loc[1]["total_annuity"], 200)
    _approx(feats.loc[1]["debt_to_income"], 0.2)   # 200 / 1000
    # case 2/3 have NO bureau record -> zeros, not NaN
    _approx(feats.loc[2]["max_dpd"], 0)
    _approx(feats.loc[2]["num_dpd_gt0"], 0)
    # unknown income -> DTI NaN (imputed later), not inf
    assert np.isnan(feats.loc[2]["debt_to_income"])


def test_no_nan_or_inf_in_feature_matrix():
    base = pd.DataFrame({"case_id": [1, 2], "target": [1, 0],
                         "WEEK_NUM": [1, 2]}).set_index("case_id")
    feats = R.assemble_features(base, pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    zero_fill = ["max_dpd", "mean_dpd", "num_dpd_gt0", "num_dpd_ge30", "max_overdue",
                 "total_overdue", "num_bureau_payments", "total_annuity",
                 "max_annuity", "num_prev_apps"]
    for c in zero_fill:
        assert not feats[c].isna().any(), f"{c} has NaN"
        assert np.isfinite(feats[c]).all(), f"{c} has inf"


def test_logreg_recovers_planted_signal():
    """End-to-end: build a matrix where max_dpd truly drives default, confirm the
    model trains cleanly (finite weights, probs in [0,1]) and finds REAL signal."""
    rng = np.random.default_rng(0)
    n = 4000
    dpd = rng.gamma(1.0, 8.0, n)
    logit = -3.0 + 0.06 * dpd
    y = (rng.random(n) < 1.0 / (1.0 + np.exp(-logit))).astype(float)
    noise = rng.normal(size=n)
    X = np.column_stack([dpd, noise])
    week = rng.integers(0, 90, n).astype(float)
    cut = np.quantile(week, 0.8)
    tr, va = week <= cut, week > cut
    Xtr, Xva = R.prepare(X[tr], X[va])
    m = R.NumpyLogReg().fit(Xtr, y[tr])
    p = m.predict_proba(Xva)
    assert np.isfinite(m.w).all()
    assert p.min() >= 0.0 and p.max() <= 1.0
    g = R.fit_eval(X, y, tr, va, [0, 1])
    assert g > 0.10, f"expected REAL signal, got Gini {g:.3f}"
    # planted-noise feature should be near-useless: dropping it barely changes Gini
    drop_noise = g - R.fit_eval(X, y, tr, va, [0])
    assert drop_noise < 0.05, f"noise feature looked important ({drop_noise:.3f})"


ALL_TESTS = [
    test_bureau_chunk_basic,
    test_bureau_dpd_capped_at_90,
    test_bureau_text_garbage_coerced,
    test_combine_bureau_true_mean_across_chunks,
    test_applprev_aggregation,
    test_assemble_fills_and_dti,
    test_no_nan_or_inf_in_feature_matrix,
    test_logreg_recovers_planted_signal,
]
