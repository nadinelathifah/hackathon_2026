"""Tier 2b -- Multi-window RECENCY features (parity-safe).

Every existing delinquency feature summarises the full 24-month window. The
single most standard Gini-mover in retail credit is RECENT-vs-LIFETIME
contrast: the last few months predict better than the lifetime average, and
the GAP between recent and lifetime behaviour captures deterioration (or
recovery) that no level feature can express.

This module adds:
  * 6-month versions of the proven DPD / overdue level features, and
  * 6m-vs-24m CONTRAST features (recent minus full window) per obligation.

PARITY DISCIPLINE (why these are parity-safe)
---------------------------------------------
Identical to feature_temporal.py and to every existing 24m feature. Windowing
uses the SAME mechanism as the rest of the library --
FeatureContext.obligations_in_window(months) filters payments to
as_of - 30.44*months <= p.date <= as_of on BOTH sources (Kaggle bureau dates
come from pmts_year/month or pmts_date; TrueLayer dates are transaction
dates). Contrast features subtract two window aggregates of the SAME payment
stream, so no new data source and no new timing assumption is introduced.

Defensive on short/empty sequences, matching the established convention:
per-obligation sequence functions return None -> that obligation is skipped;
if NO obligation qualifies the feature is 0.0 (same as the 24m features).
For CONTRAST features an obligation with no payments inside the recent window
is SKIPPED (recent behaviour undefined, not "clean") -- documented because a
reviewer will ask.

Duck-typed on FeatureContext (uses ctx.obligations_in_window, ctx.cfg);
imports only the registry decorator and CanonicalObligation -> no import
cycle.
"""
from __future__ import annotations
import statistics
from typing import Callable, List, Optional

from .canonical import CanonicalObligation
from .feature_registry import feature

_DPD_COLS = ["pmts_dpd_1073P", "pmts_dpdvalue_108P"]
_OVERDUE_COLS = ["pmts_overdue_1140A", "pmts_pmtsoverdue_635A"]
_RECENT_MONTHS = 6


# --------------------------------------------------------------------------- #
# infrastructure
# --------------------------------------------------------------------------- #
def _full_months(ctx) -> int:
    return int(ctx.cfg.default_window_months)


def _agg(ctx, months: int, seq_attr: str,
         seq_fn: Callable[[List[float]], Optional[float]]) -> float:
    """Apply a per-sequence function to each obligation's windowed sequence and
    average the non-None results across obligations (0.0 if none qualify).
    Mirrors _agg_over_obligations in feature_temporal.py."""
    vals: List[float] = []
    for o in ctx.obligations_in_window(months):
        r = seq_fn(getattr(o, seq_attr)())
        if r is not None:
            vals.append(float(r))
    return float(statistics.fmean(vals)) if vals else 0.0


def _recent_sub_obligation(o: CanonicalObligation, months: int, as_of) -> CanonicalObligation:
    """One obligation's payments restricted to the recent window, rebuilt as a
    CanonicalObligation so .dpd_values()/.overdue_amounts() apply unchanged."""
    from datetime import timedelta
    lo = as_of - timedelta(days=int(30.44 * months))
    pays = [p for p in o.payments if lo <= p.date <= as_of]
    return CanonicalObligation(o.obligation_id, o.kind, o.opened,
                               o.credit_limit, pays)


def _contrast(ctx, seq_attr: str,
              seq_fn: Callable[[List[float]], Optional[float]]) -> float:
    """Per obligation: seq_fn(recent window) - seq_fn(full window), averaged.
    Obligations with an empty recent window or a None value are SKIPPED."""
    full = _full_months(ctx)
    diffs: List[float] = []
    for o in ctx.obligations_in_window(full):
        v_full = seq_fn(getattr(o, seq_attr)())
        sub = _recent_sub_obligation(o, _RECENT_MONTHS, ctx.a.as_of)
        v_recent = seq_fn(getattr(sub, seq_attr)())
        if v_full is None or v_recent is None:
            continue
        diffs.append(float(v_recent) - float(v_full))
    return float(statistics.fmean(diffs)) if diffs else 0.0


# --------------------------------------------------------------------------- #
# per-sequence primitives (each takes ONE obligation's date-ordered sequence)
# --------------------------------------------------------------------------- #
def _seq_count_late(seq: List[float]) -> Optional[float]:
    if not seq:
        return None
    return float(sum(1 for v in seq if v > 0))


def _seq_frac_late(seq: List[float]) -> Optional[float]:
    if not seq:
        return None
    return float(sum(1 for v in seq if v > 0) / len(seq))


def _seq_max(seq: List[float]) -> Optional[float]:
    if not seq:
        return None
    return float(max(seq))


def _seq_mean(seq: List[float]) -> Optional[float]:
    if not seq:
        return None
    return float(statistics.fmean(seq))


def _seq_slope(seq: List[float]) -> Optional[float]:
    """OLS slope against the sequence's own index; None if fewer than 3 points."""
    n = len(seq)
    if n < 3:
        return None
    xs = list(range(n))
    mx = statistics.fmean(xs)
    my = statistics.fmean(seq)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    num = sum((x - mx) * (y - my) for x, y in zip(xs, seq))
    return float(num / denom)


# --------------------------------------------------------------------------- #
# registration: 6m level features  (name, mono, seq attr, seq fn, cols, desc)
# --------------------------------------------------------------------------- #
_RECENT = [
    ("num_dpd_events_6m",   +1, "dpd_values", _seq_count_late, _DPD_COLS,
     "Count of late payments in the most recent 6 months."),
    ("pct_dpd_payments_6m", +1, "dpd_values", _seq_frac_late, _DPD_COLS,
     "Share of payments made late in the most recent 6 months."),
    ("max_dpd_6m",          +1, "dpd_values", _seq_max, _DPD_COLS,
     "Worst days-past-due in the most recent 6 months."),
    ("mean_dpd_6m",         +1, "dpd_values", _seq_mean, _DPD_COLS,
     "Mean days-past-due in the most recent 6 months."),
    ("dpd_trend_slope_6m",  +1, "dpd_values", _seq_slope, _DPD_COLS,
     "OLS slope of DPD over payment order within the recent 6 months."),
    ("avg_overdue_amount_6m", +1, "overdue_amounts", _seq_mean, _OVERDUE_COLS,
     "Mean overdue amount in the most recent 6 months."),
    ("max_overdue_amount_6m", +1, "overdue_amounts", _seq_max, _OVERDUE_COLS,
     "Largest single overdue amount in the most recent 6 months."),
]


def _make_recent(seq_attr: str, seq_fn: Callable) -> Callable:
    def _f(ctx):
        return _agg(ctx, _RECENT_MONTHS, seq_attr, seq_fn)
    return _f


def _register_recency() -> int:
    n = 0
    for name, mono, attr, fn, cols, desc in _RECENT:
        feature(name, "delinquency", mono, list(cols), parity=True,
                description=f"[Tier 2b recency] {desc}")(_make_recent(attr, fn))
        n += 1
    return n


N_RECENT_FEATURES = _register_recency()


# --------------------------------------------------------------------------- #
# tolerance-aware overdue share (needs ctx.cfg.overdue_amount_tol)
# --------------------------------------------------------------------------- #
@feature("pct_overdue_payments_6m", "delinquency", +1, _OVERDUE_COLS,
         description="[Tier 2b recency] Share of payments in arrears in the most "
                     "recent 6 months (overdue > cfg.overdue_amount_tol).")
def pct_overdue_payments_6m(ctx):
    tol = ctx.cfg.overdue_amount_tol
    return _agg(ctx, _RECENT_MONTHS, "overdue_amounts",
                lambda s: (float(sum(1 for v in s if v > tol) / len(s))
                           if s else None))


# --------------------------------------------------------------------------- #
# 6m-vs-full-window CONTRAST features (recent minus lifetime; deterioration)
# --------------------------------------------------------------------------- #
@feature("pct_dpd_6m_minus_24m", "delinquency", +1, _DPD_COLS,
         description="[Tier 2b recency] Late-payment share in the recent 6m minus "
                     "the full window: positive = deteriorating, negative = "
                     "recovering. Obligations inactive recently are skipped.")
def pct_dpd_6m_minus_24m(ctx):
    return _contrast(ctx, "dpd_values", _seq_frac_late)


@feature("mean_dpd_6m_minus_24m", "delinquency", +1, _DPD_COLS,
         description="[Tier 2b recency] Mean DPD in the recent 6m minus the full "
                     "window: positive = recently later than the lifetime norm.")
def mean_dpd_6m_minus_24m(ctx):
    return _contrast(ctx, "dpd_values", _seq_mean)


@feature("avg_overdue_6m_minus_24m", "delinquency", +1, _OVERDUE_COLS,
         description="[Tier 2b recency] Mean overdue amount in the recent 6m minus "
                     "the full window: positive = arrears growing recently.")
def avg_overdue_6m_minus_24m(ctx):
    return _contrast(ctx, "overdue_amounts", _seq_mean)
