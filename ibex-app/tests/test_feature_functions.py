"""Unit tests for the maths engine, independent of any data source."""
from __future__ import annotations
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obcredit.canonical import CanonicalObligation, CanonicalPayment  # noqa: E402
from obcredit.payment_engine import (PaymentScheduleModel, RawTxn,  # noqa: E402
                                     RecurringStreamDetector, build_profile)


def _monthly_dates(start: date, n: int, late=None, period=30):
    late = late or {}
    return [start + timedelta(days=i * period + late.get(i, 0)) for i in range(n)]


def test_schedule_detects_period():
    m = PaymentScheduleModel()
    fit = m.fit(_monthly_dates(date(2025, 1, 1), 12))
    assert fit.period_days == 30
    assert all(abs(p.dpd) <= 1 for p in fit.points)


def test_late_payment_flagged():
    m = PaymentScheduleModel()
    fit = m.fit(_monthly_dates(date(2025, 1, 1), 12, late={6: 14}))
    late = [p for p in fit.points if p.is_late_outlier]
    assert len(late) == 1 and late[0].dpd >= 10


def test_skipped_cycle_counted():
    dates = _monthly_dates(date(2025, 1, 1), 12)
    del dates[5]  # skip one cycle
    fit = PaymentScheduleModel().fit(dates)
    assert fit.n_skipped_cycles >= 1


def test_profile_statistics():
    ob = CanonicalObligation("x", payments=[
        CanonicalPayment("x", d, 100.0) for d in _monthly_dates(date(2025, 1, 1), 12, late={3: 14})])
    prof = build_profile(ob)
    assert prof.n_late() == 1
    assert prof.longest_ontime_streak() >= 1
    assert max(prof.dpd_values()) >= 10


def test_detector_ignores_noise():
    txns = []
    # recurring loan: 6 monthly debits of 100
    for i in range(6):
        txns.append(RawTxn(date(2025, 1, 5) + timedelta(days=30 * i), 100.0, "acme loan", "DIRECT_DEBIT"))
    # noise: irregular varying purchases
    for i, amt in enumerate([12, 47, 5, 88, 3]):
        txns.append(RawTxn(date(2025, 1, 9) + timedelta(days=11 * i), float(amt), "tesco", "PURCHASE"))
    obs = RecurringStreamDetector().detect(txns)
    assert len(obs) == 1 and len(obs[0].payments) == 6


ALL_TESTS = [
    test_schedule_detects_period,
    test_late_payment_flagged,
    test_skipped_cycle_counted,
    test_profile_statistics,
    test_detector_ignores_noise,
]
