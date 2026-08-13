"""Parity tests: prove Kaggle f() == TrueLayer f() on identical ground truth.

These functions are pytest-compatible AND runnable by ../run_tests.py without
pytest installed. Each returns None on success and raises AssertionError on
failure.
"""
from __future__ import annotations
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from obcredit.adapters import KaggleAdapter, TrueLayerAdapter  # noqa: E402
from obcredit.config import DEFAULT  # noqa: E402
from obcredit.feature_registry import REGISTRY  # noqa: E402
from obcredit.pipeline import FeaturePipeline  # noqa: E402
from fixtures.make_fixtures import build_matched_fixtures  # noqa: E402


def _matrices():
    kaggle_frames, tl_payloads = build_matched_fixtures()
    pipe = FeaturePipeline()
    k = pipe.build_matrix(KaggleAdapter(kaggle_frames).to_canonical())
    t = pipe.build_matrix(TrueLayerAdapter(tl_payloads).to_canonical())
    return k, t


def _close(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, float) and math.isnan(a) and isinstance(b, float) and math.isnan(b):
        return True
    return abs(float(a) - float(b)) <= max(DEFAULT.parity_abs_tol,
                                           DEFAULT.parity_rel_tol * max(abs(float(a)), abs(float(b))))


def test_same_cases_present():
    k, t = _matrices()
    assert list(k.index) == list(t.index), (list(k.index), list(t.index))


def test_parity_features_match():
    k, t = _matrices()
    parity_cols = REGISTRY.parity_names()
    mismatches = []
    for case_id in k.index:
        for col in parity_cols:
            a, b = k.loc[case_id, col], t.loc[case_id, col]
            if not _close(a, b):
                mismatches.append((case_id, col, a, b))
    assert not mismatches, "PARITY MISMATCH:\n" + "\n".join(
        f"  case={c} feature={f}: kaggle={a} truelayer={b}" for c, f, a, b in mismatches)


def test_late_payer_has_signal():
    """Sanity: the arrears-heavy applicant (1002) must look riskier than 1001."""
    k, _ = _matrices()
    assert k.loc["1002", "max_overdue_amount_24m"] > k.loc["1001", "max_overdue_amount_24m"]
    assert k.loc["1002", "num_overdue_payments_24m"] >= 3
    assert k.loc["1002", "total_overdue_amount"] > k.loc["1001", "total_overdue_amount"]


def test_income_parity():
    k, t = _matrices()
    for case_id in k.index:
        assert _close(k.loc[case_id, "monthly_income"], t.loc[case_id, "monthly_income"]), case_id


def test_declared_features():
    """Declared onboarding features are built AND identical across sources."""
    k, t = _matrices()
    # 1001 is an employed homeowner; 1002 is neither.
    assert k.loc["1001", "declared_is_homeowner"] == 1.0
    assert k.loc["1002", "declared_is_homeowner"] == 0.0
    assert k.loc["1001", "declared_income_is_employment"] == 1.0
    assert k.loc["1002", "declared_income_is_employment"] == 0.0
    # 1001 declared income matches observed (gap ~0); 1002 overstates (gap > 0).
    assert k.loc["1001", "declared_income_gap"] < 1e-9
    assert k.loc["1002", "declared_income_gap"] > 0.0
    # parity: every declared feature identical Kaggle vs TrueLayer.
    declared_cols = ["declared_is_homeowner", "declared_income_is_employment",
                     "declared_income_type_code", "declared_education_code",
                     "declared_housing_code", "declared_employment_code",
                     "declared_income_gap"]
    for cid in k.index:
        for col in declared_cols:
            assert _close(k.loc[cid, col], t.loc[cid, col]), (
                cid, col, k.loc[cid, col], t.loc[cid, col])


ALL_TESTS = [
    test_same_cases_present,
    test_parity_features_match,
    test_late_payer_has_signal,
    test_income_parity,
    test_declared_features,
]
