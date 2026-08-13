"""Income-detection tests -- prove the RECURRING-INFLOW detector is robust and
does NOT latch onto one-off / tiny credits (the old keyword+median ~£25 bug).

Runnable by ../run_tests.py without pytest. Each returns None on success and
raises AssertionError on failure.
"""
from __future__ import annotations
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obcredit.adapters.truelayer_adapter import TrueLayerAdapter  # noqa: E402

AS_OF = date(2025, 6, 1)


def _credit(day_offset, amount, desc, merchant, cat="CREDIT"):
    d = AS_OF - timedelta(days=day_offset)
    return {"timestamp": d.isoformat() + "T00:00:00Z", "amount": float(amount),
            "description": desc, "merchant_name": merchant,
            "transaction_category": cat}


def _adapter():
    return TrueLayerAdapter([])


def _salary(n=12, amount=2500.0, period=30):
    return [_credit(period * (n - 1 - i), amount, "ACME CORP SALARY", "ACME CORP")
            for i in range(n)]


def test_detects_monthly_salary_exact():
    # a ~monthly cadence must return the amount UNCHANGED (parity-safe).
    inc = _adapter()._detect_income(_salary(12, 2500.0))
    assert inc == 2500.0, inc


def test_ignores_oneoff_paypal_and_noise():
    # a single 25 PayPal credit + a couple of irregular refunds, NO salary.
    txns = [
        _credit(10, 25.0, "PAYPAL PAYMENT", "PAYPAL"),
        _credit(40, 12.0, "REFUND", "AMAZON"),
        _credit(55, 9.99, "REPAYMENT ADJ", "KLARNA"),
    ]
    assert _adapter()._detect_income(txns) is None


def test_salary_wins_over_oneoff_noise():
    txns = _salary(12, 2600.0) + [_credit(3, 25.0, "PAYPAL PAYMENT", "PAYPAL")]
    assert _adapter()._detect_income(txns) == 2600.0


def test_rejects_sub_floor_recurring():
    # a recurring 25 monthly credit that is NOT salary and below the floor.
    txns = [_credit(30 * (11 - i), 25.0, "SPOTIFY REFUND", "SPOTIFY")
            for i in range(12)]
    assert _adapter()._detect_income(txns) is None


def test_fallback_to_largest_recurring_without_keyword():
    # a recurring monthly credit above the floor, no salary keyword -> used.
    txns = [_credit(30 * (11 - i), 1800.0, "MONTHLY TRANSFER", "J SMITH")
            for i in range(12)]
    assert _adapter()._detect_income(txns) == 1800.0


def test_weekly_cadence_scaled_to_monthly():
    # 500/week -> ~2174/month (500 * 30.44 / 7).
    txns = [_credit(7 * (11 - i), 500.0, "WEEKLY WAGES", "GIGCO")
            for i in range(12)]
    inc = _adapter()._detect_income(txns)
    assert inc is not None and 2100.0 < inc < 2250.0, inc


def test_prefers_salary_over_larger_nonsalary_stream():
    # a bigger recurring non-salary transfer must NOT beat the named salary.
    txns = _salary(12, 2600.0) + [
        _credit(30 * (11 - i), 4000.0, "SAVINGS SWEEP", "J SMITH")
        for i in range(12)]
    assert _adapter()._detect_income(txns) == 2600.0


ALL_TESTS = [
    test_detects_monthly_salary_exact,
    test_ignores_oneoff_paypal_and_noise,
    test_salary_wins_over_oneoff_noise,
    test_rejects_sub_floor_recurring,
    test_fallback_to_largest_recurring_without_keyword,
    test_weekly_cadence_scaled_to_monthly,
    test_prefers_salary_over_larger_nonsalary_stream,
]
