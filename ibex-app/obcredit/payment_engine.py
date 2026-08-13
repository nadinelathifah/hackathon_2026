"""The shared maths engine: how raw payment events become a delinquency signal.

Three OOP components, used IDENTICALLY by both adapters:

  RecurringStreamDetector  -- raw transactions -> recurring obligations
                              (only needed by the TrueLayer side; Kaggle gives
                               us the contract grouping for free).
  PaymentScheduleModel     -- a list of payment dates -> an inferred cyclical
                              schedule and a per-payment Days-Past-Due (DPD)
                              series, via a robust 'outlier' method.
  DelinquencyProfile       -- wraps a DPD series and exposes the statistics the
                              feature layer needs (max, mean, counts, streaks...).

Why infer the schedule instead of trusting a stated due date?
  In open banking we rarely see the contractual due date -- only the cash that
  moved. The defensible, bank-grade approach is to LEARN the customer's payment
  cadence robustly and measure deviations from it. We use the median inter-payment
  interval as the period (resistant to a few irregular gaps) and Median Absolute
  Deviation (MAD) to flag genuine late-payment outliers rather than normal jitter.
  We then apply the EXACT same model to the Kaggle payment dates, so the signal
  is constructed the same way on both sides.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple
import math
import statistics

from .canonical import CanonicalObligation, CanonicalPayment
from .config import DEFAULT, EngineConfig
from .logging_utils import get_logger

log = get_logger("payment_engine")


# --------------------------------------------------------------------------- #
# small robust-statistics helpers
# --------------------------------------------------------------------------- #
def _median(xs: Sequence[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return float(statistics.median(xs)) if xs else None


def _mad(xs: Sequence[float], scale: float) -> float:
    """Median Absolute Deviation, scaled to be a robust sigma estimate."""
    if len(xs) < 2:
        return 0.0
    med = statistics.median(xs)
    dev = [abs(x - med) for x in xs]
    return float(statistics.median(dev)) * scale


def _cv(xs: Sequence[float]) -> float:
    """Coefficient of variation = sd / |mean|. 0 when flat, large when noisy."""
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return 0.0
    mean = statistics.fmean(xs)
    if mean == 0:
        return 0.0
    return float(statistics.pstdev(xs) / abs(mean))


# --------------------------------------------------------------------------- #
# 1) recurring-stream detection (TrueLayer transactions -> obligations)
# --------------------------------------------------------------------------- #
@dataclass
class RawTxn:
    """Minimal transaction the detector needs (adapter fills this)."""
    date: date
    amount: float            # POSITIVE = money leaving the account (a debit)
    counterparty: str        # normalised merchant / payee key
    kind_hint: str = "unknown"


LOAN_KEYWORDS = ("loan", "finance", "lending", "credit", "repay", "instal")
CARD_KEYWORDS = ("card", "visa", "mastercard", "amex")
BNPL_KEYWORDS = ("klarna", "clearpay", "laybuy", "paypal4", "zilch", "bnpl")


class RecurringStreamDetector:
    """Group debit transactions into recurring obligations.

    Method (rule-based, transparent, auditable -- preferred over a black box for
    a regulated model): cluster by normalised counterparty, then by similar
    amount, then require enough payments at a stable cadence. This mirrors the
    DBSCAN-on-(amount, periodicity) approaches in the open-banking literature
    but stays fully explainable.
    """

    def __init__(self, cfg: EngineConfig = DEFAULT):
        self.cfg = cfg

    def _classify(self, counterparty: str, hint: str) -> str:
        text = f"{counterparty} {hint}".lower()
        if any(k in text for k in BNPL_KEYWORDS):
            return "bnpl"
        if any(k in text for k in CARD_KEYWORDS):
            return "card"
        if any(k in text for k in LOAN_KEYWORDS):
            return "loan"
        return "unknown"

    def detect(self, txns: Sequence[RawTxn]) -> List[CanonicalObligation]:
        debits = [t for t in txns if t.amount > 0]
        # bucket by (counterparty, rounded amount)
        buckets: Dict[Tuple[str, float], List[RawTxn]] = {}
        for t in debits:
            key = (t.counterparty, round(t.amount, self.cfg.amount_round_dp))
            buckets.setdefault(key, []).append(t)

        obligations: List[CanonicalObligation] = []
        for (cp, _amt), group in buckets.items():
            if len(group) < self.cfg.min_payments_for_stream:
                continue
            dates = sorted(t.date for t in group)
            intervals = [(b - a).days for a, b in zip(dates, dates[1:])]
            if not intervals:
                continue
            period = statistics.median(intervals)
            if not (self.cfg.period_min_days <= period <= self.cfg.period_max_days):
                continue
            if _cv([t.amount for t in group]) > self.cfg.amount_cv_max:
                continue
            kind = self._classify(cp, group[0].kind_hint)
            ob = CanonicalObligation(
                obligation_id=f"stream::{cp}",
                kind=kind,
                opened=min(dates),
                payments=[CanonicalPayment(f"stream::{cp}", t.date, t.amount) for t in group],
            )
            obligations.append(ob)
            log.debug("stream %s kind=%s n=%d period=%.1fd", cp, kind, len(group), period)
        return obligations


# --------------------------------------------------------------------------- #
# 2) cyclical-payment schedule model + DPD (the 'outlier method')
# --------------------------------------------------------------------------- #
@dataclass
class SchedulePoint:
    actual: date
    expected: date
    dpd: int                 # actual - expected, in days (negative = early)
    is_late_outlier: bool


@dataclass
class ScheduleFit:
    period_days: Optional[float]
    anchor: Optional[date]
    points: List[SchedulePoint] = field(default_factory=list)
    n_skipped_cycles: int = 0


class PaymentScheduleModel:
    """Learn a customer's payment cadence and score each payment's lateness.

    Steps:
      1. period = median of inter-payment intervals (robust to occasional gaps).
      2. snap each actual payment to the nearest expected grid slot
         (anchor + k*period); DPD = (actual - slot).days.
      3. count grid slots inside the observed span that have NO payment within
         half a period -> skipped/missed cycles (capped DPD contribution).
      4. flag late OUTLIERS using a MAD robust z-score on positive DPDs, so we
         distinguish a genuinely late payment from normal a-few-days jitter.
    """

    def __init__(self, cfg: EngineConfig = DEFAULT):
        self.cfg = cfg

    def _tol_days(self, period: float) -> float:
        return max(self.cfg.ontime_abs_tol_days, self.cfg.ontime_rel_tol * period)

    def fit(self, dates: Sequence[date]) -> ScheduleFit:
        ds = sorted(set(dates))
        if len(ds) < 2:
            return ScheduleFit(period_days=None, anchor=ds[0] if ds else None)
        anchor = ds[0]
        offsets = [(d - anchor).days for d in ds]
        diffs = [b - a for a, b in zip(offsets, offsets[1:])]
        period = float(statistics.median(diffs))
        if period <= 0:
            return ScheduleFit(period_days=None, anchor=anchor)

        # Assign each payment to a CYCLE INDEX rather than snapping it to the
        # nearest grid slot. A gap of ~k*period advances the cycle counter by k,
        # so (k-1) skipped cycles are recorded and the *next* real payment lands
        # back on dpd~0. This keeps on-time payments at 0 DPD and measures the
        # genuine lateness of a payment without a late payment being absorbed
        # into the following slot (the failure mode of pure nearest-slot snapping).
        cycles = [0]
        n_skipped = 0
        for gap in diffs:
            step = max(1, int(round(gap / period)))
            cycles.append(cycles[-1] + step)
            n_skipped += step - 1

        points: List[SchedulePoint] = []
        for off, cyc, d in zip(offsets, cycles, ds):
            expected_off = int(round(cyc * period))
            slot = anchor + timedelta(days=expected_off)
            dpd = off - expected_off
            points.append(SchedulePoint(actual=d, expected=slot, dpd=dpd, is_late_outlier=False))

        # robust late-outlier flagging on positive DPDs (MAD z-score)
        pos = [p.dpd for p in points if p.dpd > 0]
        tol = self._tol_days(period)
        if pos:
            med = statistics.median(pos)
            mad = _mad(pos, self.cfg.mad_scale)
            for p in points:
                if p.dpd <= tol:
                    continue
                if mad == 0:
                    p.is_late_outlier = p.dpd > tol
                else:
                    z = (p.dpd - med) / mad
                    p.is_late_outlier = (z >= self.cfg.robust_z_threshold) or (p.dpd > tol)

        return ScheduleFit(period_days=period, anchor=anchor, points=points,
                           n_skipped_cycles=n_skipped)


# --------------------------------------------------------------------------- #
# 3) delinquency profile (consumed by the feature layer)
# --------------------------------------------------------------------------- #
class DelinquencyProfile:
    """All delinquency statistics derived from one obligation's schedule fit.

    DPD convention for downstream features: floored at 0 (a payment made early
    is 'on time', not negative DPD), and a skipped cycle contributes the capped
    missed-DPD value.
    """

    def __init__(self, fit: ScheduleFit, cfg: EngineConfig = DEFAULT):
        self.fit = fit
        self.cfg = cfg
        self.dpd_series: List[Tuple[date, int]] = [
            (p.actual, max(0, p.dpd)) for p in fit.points
        ]

    # primitive series ------------------------------------------------------
    def dpd_values(self) -> List[int]:
        vals = [v for _, v in self.dpd_series]
        vals += [self.cfg.missed_dpd_cap_days] * self.fit.n_skipped_cycles
        return vals

    def n_payments(self) -> int:
        return len(self.fit.points)

    def n_late(self) -> int:
        return sum(1 for p in self.fit.points if p.is_late_outlier)

    def n_missed(self) -> int:
        return self.fit.n_skipped_cycles

    # shape statistics ------------------------------------------------------
    def longest_ontime_streak(self) -> int:
        best = cur = 0
        for p in self.fit.points:
            if p.is_late_outlier:
                cur = 0
            else:
                cur += 1
                best = max(best, cur)
        return best

    def days_to_recover(self) -> Optional[int]:
        """Average days between a late-outlier payment and the next on-time one."""
        gaps: List[int] = []
        pts = self.fit.points
        for i, p in enumerate(pts):
            if p.is_late_outlier:
                for q in pts[i + 1:]:
                    if not q.is_late_outlier:
                        gaps.append((q.actual - p.actual).days)
                        break
        return int(round(statistics.fmean(gaps))) if gaps else None

    def interval_std_days(self) -> Optional[float]:
        ds = [p.actual for p in self.fit.points]
        if len(ds) < 3:
            return None
        intervals = [(b - a).days for a, b in zip(ds, ds[1:])]
        return float(statistics.pstdev(intervals))

    def dpd_trend_slope(self) -> Optional[float]:
        """OLS slope of DPD over payment index: is the customer deteriorating?"""
        ys = [v for _, v in self.dpd_series]
        n = len(ys)
        if n < 3:
            return None
        xs = list(range(n))
        mx = statistics.fmean(xs)
        my = statistics.fmean(ys)
        denom = sum((x - mx) ** 2 for x in xs)
        if denom == 0:
            return 0.0
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        return float(num / denom)


def build_profile(obligation: CanonicalObligation, cfg: EngineConfig = DEFAULT) -> DelinquencyProfile:
    model = PaymentScheduleModel(cfg)
    fit = model.fit(obligation.payment_dates())
    return DelinquencyProfile(fit, cfg)
