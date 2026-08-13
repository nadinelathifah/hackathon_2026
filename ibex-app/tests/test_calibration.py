"""Tests for isotonic calibration + PDO scorecard (run by ../run_tests.py).

Pure NumPy; each returns None on success and raises AssertionError on failure.
"""
from __future__ import annotations
import os
import pickle
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from obcredit.modeling.calibration import (IsotonicCalibrator, brier_score,  # noqa: E402
                                           expected_calibration_error,
                                           reliability_table, BASEL_PD_FLOOR)
from obcredit.modeling.scorecard import (pd_to_score, score_to_band,  # noqa: E402
                                         top_reason_codes, DEFAULT_BASE_SCORE,
                                         DEFAULT_BASE_ODDS)
from obcredit.modeling.metrics import gini  # noqa: E402


def _synth(n=2000, seed=0):
    rng = np.random.RandomState(seed)
    score = rng.rand(n)
    pd_true = 0.05 + 0.9 * score ** 1.5
    y = (rng.rand(n) < pd_true).astype(float)
    return score, y


def _plateaued(n=40000, seed=3, grid=50):
    """Reproduces the production failure mode: PAVA pools the low-score end into
    a wide flat block, so every strong applicant is pinned to the same PD and
    the credit score is hard-capped. Scores are quantised so each knot carries
    plenty of observations (as they do after LightGBM scoring)."""
    rng = np.random.RandomState(seed)
    score = np.round(rng.rand(n) * grid) / grid
    pd_true = np.where(score < 0.35, 0.06,
                       0.06 + 1.2 * np.clip(score - 0.35, 0.0, None) ** 1.4)
    y = (rng.rand(n) < np.clip(pd_true, 0.0, 1.0)).astype(float)
    return score, y


def test_isotonic_monotone():
    score, y = _synth()
    cal = IsotonicCalibrator().fit(score, y)
    pred = cal.predict(np.linspace(0, 1, 50))
    assert np.all(np.diff(pred) >= -1e-9), "calibrated PD must be non-decreasing"


def test_isotonic_improves_brier():
    score, y = _synth()
    cal = IsotonicCalibrator().fit(score, y)
    raw_b = brier_score(score, y)
    cal_b = brier_score(cal.predict(score), y)
    assert cal_b <= raw_b + 1e-9, (raw_b, cal_b)


def test_isotonic_preserves_ranking():
    score, y = _synth()
    cal = IsotonicCalibrator().fit(score, y)
    g_raw = gini(y, score)
    g_cal = gini(y, cal.predict(score))
    assert abs(g_raw - g_cal) < 0.02, (g_raw, g_cal)


def test_reliability_shape():
    score, y = _synth()
    cal = IsotonicCalibrator().fit(score, y)
    tbl = reliability_table(cal.predict(score), y, n_bins=10)
    assert len(tbl) == 10
    assert all(len(row) == 4 for row in tbl)


def test_scorecard_monotone_and_bands():
    pds = np.array([0.001, 0.01, 0.05, 0.2, 0.5, 0.9])
    scores = pd_to_score(pds)
    assert np.all(np.diff(scores) < 0), "score must fall as PD rises"
    anchor_pd = 1.0 / (DEFAULT_BASE_ODDS + 1.0)   # odds_good = base_odds
    assert abs(pd_to_score(anchor_pd) - DEFAULT_BASE_SCORE) < 1e-6
    bands = score_to_band(scores)
    assert len(bands) == len(pds)
    assert bands[0] == "A"


def test_scorecard_scalar_and_reasons():
    s = pd_to_score(0.05)
    assert isinstance(s, float)
    assert score_to_band(s) in {"A", "B", "C", "D", "E"}
    reasons = top_reason_codes([0.1, -0.3, 0.5, 0.2], ["a", "b", "c", "d"], k=2)
    assert [r[0] for r in reasons] == ["c", "d"]


def test_isotonic_tail_decaps():
    """loglinear tail must push PD BELOW the lowest calibration bin for a
    stronger-than-seen score (and ABOVE the highest for a weaker-than-seen one),
    so the credit score is no longer capped at the in-sample PD floor. Built on a
    fixed calibration curve (per-point isotonic on binary data can leave y_[0]=0,
    which is not the property under test)."""
    x = np.array([0.2, 0.4, 0.6, 0.8])
    y = np.array([0.05, 0.20, 0.50, 0.80])   # nonzero, monotone boundary PDs
    cal = IsotonicCalibrator(tail="loglinear", pd_floor=1e-4)
    cal.x_, cal.y_ = x.copy(), y.copy()
    clamp = IsotonicCalibrator(tail="clamp")
    clamp.x_, clamp.y_ = x.copy(), y.copy()
    lo_pd = float(cal.predict(np.array([-1.0]))[0])   # far below observed support
    hi_pd = float(cal.predict(np.array([2.0]))[0])     # far above observed support
    # de-capped: strictly beyond the boundary the clamp would have pinned us to
    assert lo_pd < float(cal.y_[0]) - 1e-6, (lo_pd, float(cal.y_[0]))
    assert hi_pd > float(cal.y_[-1]) + 1e-6, (hi_pd, float(cal.y_[-1]))
    # clamp mode still pins to the boundary (legacy behaviour preserved)
    assert abs(float(clamp.predict(np.array([-1.0]))[0]) - float(y[0])) < 1e-9
    assert abs(float(clamp.predict(np.array([2.0]))[0]) - float(y[-1])) < 1e-9
    # PD floor respected and curve stays monotone across a wide grid
    grid = cal.predict(np.linspace(-2.0, 2.0, 400))
    assert grid.min() >= 1e-4 - 1e-12 and grid.max() <= 1.0 - 1e-4 + 1e-12
    assert np.all(np.diff(grid) >= -1e-9), "extrapolated curve must stay monotone"


def test_isotonic_clamp_mode_still_bounded():
    """Legacy clamp mode holds the boundary PD flat outside the support."""
    score, y = _synth()
    cal = IsotonicCalibrator(tail="clamp").fit(score, y)
    below = float(cal.predict(np.array([float(score.min()) - 1.0]))[0])
    assert abs(below - float(cal.y_[0])) < 1e-6, (below, float(cal.y_[0]))


# --------------------------------------------------------------- BUILD 18


def test_hybrid_breaks_in_support_plateau():
    """THE headline fix. Inside the support, hybrid must price below the flat
    isotonic plateau, so the credit score is no longer hard-capped."""
    score, y = _plateaued()
    cal = IsotonicCalibrator(tail="hybrid").fit(score, y)
    lead, _ = cal._terminal_runs()
    assert lead >= 2, ("fixture must actually produce a plateau", lead)
    iso_min = float(cal.y_[0])
    pd_min = float(cal.predict(score).min())
    assert pd_min < iso_min - 1e-6, (pd_min, iso_min)
    # and that shows up as real headroom in the credit score
    gained = float(pd_to_score(pd_min)) - float(pd_to_score(iso_min))
    assert gained > 10.0, ("score should be materially de-capped", gained)


def test_break_plateau_false_reproduces_build17():
    """The override is a POLICY switch. Turned off, nothing inside the observed
    support is rewritten -- BUILD 17 behaviour, exactly."""
    score, y = _plateaued()
    cal = IsotonicCalibrator(tail="hybrid", break_plateau=False).fit(score, y)
    s = np.linspace(float(cal.x_[0]), float(cal.x_[-1]), 500)
    interp = np.interp(s, cal.x_, cal.y_)
    got = cal.predict(s)
    floor = float(cal.pd_floor)
    assert np.allclose(got, np.clip(interp, floor, 1.0 - floor), atol=1e-12)


def test_hybrid_monotone_and_ranking_preserved():
    """Discrimination is untouched: the map is monotone, so Gini cannot move
    except through isotonic tie-breaking."""
    score, y = _plateaued()
    cal = IsotonicCalibrator(tail="hybrid").fit(score, y)
    grid = cal.predict(np.linspace(-0.5, 1.5, 800))
    assert np.all(np.diff(grid) >= -1e-9), "hybrid curve must stay monotone"
    g_raw = gini(y, score)
    g_cal = gini(y, cal.predict(score))
    assert abs(g_raw - g_cal) < 0.02, (g_raw, g_cal)


def test_hybrid_leaves_interior_isotonic_untouched():
    """Only the terminal plateaus may be rewritten. Observed interior bins are
    real data and must survive verbatim."""
    score, y = _plateaued()
    cal = IsotonicCalibrator(tail="hybrid").fit(score, y)
    lead, trail = cal._terminal_runs()
    s = np.linspace(float(cal.x_[lead]), float(cal.x_[trail]), 300)
    interp = np.interp(s, cal.x_, cal.y_)
    assert np.allclose(cal.predict(s), interp, atol=1e-12)


def test_central_tendency_reanchor():
    """Basel: mean predicted PD must equal the observed long-run default rate.
    De-capping the tail pushes mean PD down, so it has to be re-anchored -- and
    because the shift is monotone it must not disturb the ranking."""
    score, y = _plateaued()
    cal = IsotonicCalibrator(tail="hybrid").fit(score, y)
    target = float(y.mean())
    before = gini(y, cal.predict(score))
    cal.fit_central_tendency(score, target)
    got = float(cal.predict(score).mean())
    assert abs(got - target) < 1e-6, (got, target)
    after = gini(y, cal.predict(score))
    assert abs(before - after) < 1e-6, (before, after)


def test_moc_is_conservative_only_where_extrapolated():
    """Margin of Conservatism must raise PD where we extrapolated, and nowhere
    else. Applying it to observed bins would be double-counting."""
    score, y = _plateaued()
    plain = IsotonicCalibrator(tail="hybrid").fit(score, y)
    moc = IsotonicCalibrator(tail="hybrid", moc_logodds=0.25).fit(score, y)
    p0, replaced = plain._predict_raw(score)
    p1, _ = moc._predict_raw(score)
    assert replaced.any(), "fixture should extrapolate somewhere"
    assert np.all(p1[replaced] > p0[replaced] - 1e-12)
    assert (p1[replaced] > p0[replaced] + 1e-9).any(), "MoC must bite"
    assert np.allclose(p1[~replaced], p0[~replaced], atol=1e-12), \
        "MoC must not touch observed bins"


def test_basel_pd_floor_default():
    """BUILD <=17 defaulted to a 1bp floor, BELOW the CRR Art.160/163 floor of
    3bp. That was a real compliance defect."""
    assert abs(BASEL_PD_FLOOR - 0.0003) < 1e-12
    cal = IsotonicCalibrator()
    assert abs(cal.pd_floor - BASEL_PD_FLOOR) < 1e-12
    score, y = _plateaued()
    cal.fit(score, y)
    p = cal.predict(np.linspace(-5.0, 5.0, 400))
    assert p.min() >= BASEL_PD_FLOOR - 1e-12
    assert p.max() <= 1.0 - BASEL_PD_FLOOR + 1e-12


def test_diagnose_reports_backbone_share():
    """You cannot sign off what you cannot see: diagnose() must say how much of
    the book is priced off the extrapolated backbone rather than observed data."""
    score, y = _plateaued()
    cal = IsotonicCalibrator(tail="hybrid").fit(score, y)
    d = cal.diagnose(score)
    for key in ("frac_backbone_priced", "lower_plateau_knots", "pd_min",
                "mean_pd", "backbone_slope", "lower_plateau_z",
                "break_plateau", "moc_logodds", "ct_shift"):
        assert key in d, key
    assert 0.0 < d["frac_backbone_priced"] <= 1.0
    assert d["pd_min"] < d["isotonic_pd_min"] + 1e-12


def test_legacy_pickle_without_counts_degrades_safely():
    """A BUILD <=17 calibrator.pkl carries no per-knot counts, so no backbone can
    be fitted. It must still load and predict, falling back to loglinear rather
    than inventing a trend."""
    path = os.path.join(tempfile.mkdtemp(), "legacy.pkl")
    with open(path, "wb") as f:
        pickle.dump({"x": [0.2, 0.4, 0.6, 0.8], "y": [0.05, 0.2, 0.5, 0.8],
                     "tail": "hybrid", "pd_floor": 1e-4}, f)
    cal = IsotonicCalibrator.load(path)
    assert cal.backbone_ is None
    p = cal.predict(np.linspace(-1.0, 2.0, 200))
    assert np.all(np.isfinite(p))
    assert np.all(np.diff(p) >= -1e-9)


def test_expected_calibration_error_sane():
    """ECE is equal-COUNT binned; a perfectly calibrated predictor scores ~0 and
    a deliberately biased one scores clearly worse."""
    rng = np.random.RandomState(7)
    p = rng.rand(20000) * 0.5
    y = (rng.rand(20000) < p).astype(float)
    good = expected_calibration_error(p, y, n_bins=20)
    bad = expected_calibration_error(np.clip(p + 0.2, 0, 1), y, n_bins=20)
    assert good < 0.02, good
    assert bad > good + 0.1, (good, bad)


ALL_TESTS = [
    test_isotonic_monotone,
    test_isotonic_improves_brier,
    test_isotonic_preserves_ranking,
    test_reliability_shape,
    test_scorecard_monotone_and_bands,
    test_scorecard_scalar_and_reasons,
    test_isotonic_tail_decaps,
    test_isotonic_clamp_mode_still_bounded,
    test_hybrid_breaks_in_support_plateau,
    test_break_plateau_false_reproduces_build17,
    test_hybrid_monotone_and_ranking_preserved,
    test_hybrid_leaves_interior_isotonic_untouched,
    test_central_tendency_reanchor,
    test_moc_is_conservative_only_where_extrapolated,
    test_basel_pd_floor_default,
    test_diagnose_reports_backbone_share,
    test_legacy_pickle_without_counts_degrades_safely,
    test_expected_calibration_error_sane,
]
