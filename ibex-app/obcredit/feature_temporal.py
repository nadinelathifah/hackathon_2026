"""Tier 2 -- Temporal / trajectory features (parity-safe).

The Tier 1 aggregation grammar (count/mean/max/...) summarises the DPD stream
but DISCARDS order and timing. The BUILD 11 run proved that: every order-less
agg_dpd_* value-op landed at +0.0000 permutation importance because the static
level of the DPD stream was already saturated by the hand-written features.

This module extracts the ORTHOGONAL information the aggregations threw away --
the SHAPE of the delinquency sequence over time:

  * trajectory   -- is lateness accelerating? recent vs older?
  * recency      -- how recent is the lateness / how long clean right now?
  * persistence  -- does lateness cluster (autocorrelation of the late flag)?
  * volatility   -- how erratic is the payment behaviour?

PARITY DISCIPLINE (why these are parity-safe)
---------------------------------------------
Every feature is computed PER OBLIGATION on that obligation's date-ordered
DPD sequence (CanonicalObligation.dpd_values()), then averaged across
obligations -- exactly the pattern dpd_trend_slope_24m already uses and that
the parity suite already proves matches to 1e-6. Crucially they use the
sequence's own INDEX (payment position), never absolute calendar dates: the
DPD *values* reconstruct elementwise-identically from Kaggle and TrueLayer, but
the absolute payment *dates* do not (the bureau uses a monthly grid), so any
date-based temporal feature would have to be parity=False. Index-based ->
parity-safe.

All features are defensive on short/empty sequences (return None so the
applicant-level mean simply skips that obligation; 0.0 if nothing qualifies).
Duck-typed on FeatureContext (uses ctx.obligations_in_window, ctx.cfg); imports
only the registry decorator, so there is no import cycle.
"""
from __future__ import annotations
import math
import statistics
from typing import Callable, List, Optional

from .feature_registry import feature

_DPD_COLS = ["pmts_dpd_1073P", "pmts_dpdvalue_108P"]


# --------------------------------------------------------------------------- #
# infrastructure
# --------------------------------------------------------------------------- #
def _window(ctx):
    return ctx.obligations_in_window(ctx.cfg.default_window_months)


def _agg_over_obligations(ctx, seq_fn: Callable[[List[float]], Optional[float]]) -> float:
    """Apply a per-sequence function to each obligation's DPD sequence and
    average the non-None results across obligations (0.0 if none qualify).
    This mirrors dpd_trend_slope_24m, the established parity-safe pattern."""
    vals: List[float] = []
    for o in _window(ctx):
        r = seq_fn(o.dpd_values())
        if r is not None:
            vals.append(float(r))
    return float(statistics.fmean(vals)) if vals else 0.0


def _ols_slope(ys: List[float]) -> Optional[float]:
    """OLS slope of ys against its own index (0..n-1); None if too short."""
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


def _binary_late(seq: List[float]) -> List[float]:
    return [1.0 if v > 0 else 0.0 for v in seq]


# --------------------------------------------------------------------------- #
# per-sequence primitives (each takes ONE obligation's date-ordered DPD list)
# --------------------------------------------------------------------------- #
def _seq_acceleration(seq: List[float]) -> Optional[float]:
    """Change in trend: slope of the recent half minus slope of the older half.
    Positive => lateness is worsening at an increasing rate. Needs >=6 payments
    (each half needs >=3 for a slope)."""
    n = len(seq)
    if n < 6:
        return None
    mid = n // 2
    s_old = _ols_slope(seq[:mid])
    s_new = _ols_slope(seq[mid:])
    if s_old is None or s_new is None:
        return None
    return s_new - s_old


def _seq_recent_minus_old(seq: List[float]) -> Optional[float]:
    """Mean DPD of the recent half minus the older half. Positive => recently
    later than before (deterioration), independent of a linear-slope shape."""
    n = len(seq)
    if n < 4:
        return None
    mid = n // 2
    old = seq[:mid]
    new = seq[mid:]
    if not old or not new:
        return None
    return statistics.fmean(new) - statistics.fmean(old)


def _seq_recency_weighted(seq: List[float], decay: float = 0.85) -> Optional[float]:
    """Exponentially recency-weighted mean DPD: the most recent payment carries
    the most weight (weight_i = decay**(n-1-i)). Captures 'how bad LATELY'
    rather than 'how bad on average'."""
    n = len(seq)
    if n == 0:
        return None
    num = 0.0
    den = 0.0
    for i, v in enumerate(seq):
        w = decay ** (n - 1 - i)
        num += w * v
        den += w
    return num / den if den else None


def _seq_frac_late_recent_third(seq: List[float]) -> Optional[float]:
    """Share of late payments confined to the most recent third of history."""
    n = len(seq)
    if n < 3:
        return None
    k = max(1, n // 3)
    recent = seq[-k:]
    return sum(1 for v in recent if v > 0) / len(recent)


def _seq_trailing_clean(seq: List[float]) -> Optional[float]:
    """Current clean streak: number of consecutive on-time payments at the END
    of the sequence (how long the line has been clean right now)."""
    if not seq:
        return None
    c = 0
    for v in reversed(seq):
        if v <= 0:
            c += 1
        else:
            break
    return float(c)


def _seq_mean_abs_change(seq: List[float]) -> Optional[float]:
    """Mean absolute successive change in DPD -- volatility of lateness
    (erratic payers vs steadily-late payers with the same average)."""
    if len(seq) < 2:
        return None
    diffs = [abs(seq[i] - seq[i - 1]) for i in range(1, len(seq))]
    return statistics.fmean(diffs)


def _seq_autocorr(seq: List[float], lag: int) -> Optional[float]:
    """Lag-k autocorrelation of the binary late indicator: does being late one
    cycle predict being late k cycles later? Positive => lateness clusters
    (persistent distress) rather than one-off slips."""
    b = _binary_late(seq)
    n = len(b)
    if n < lag + 2:
        return None
    mb = statistics.fmean(b)
    var = sum((v - mb) ** 2 for v in b)
    if var == 0:
        return 0.0
    cov = sum((b[i] - mb) * (b[i - lag] - mb) for i in range(lag, n))
    return cov / var


def _seq_late_entropy(seq: List[float]) -> Optional[float]:
    """Shannon entropy (bits) of the binary late indicator: how UNPREDICTABLE
    the on-time/late behaviour is. Peaks at a 50/50 mix; 0 for always-on-time
    or always-late. Direction is ambiguous, so left unconstrained (mono 0)."""
    b = _binary_late(seq)
    n = len(b)
    if n < 2:
        return None
    p = sum(b) / n
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return -(p * math.log2(p) + (1 - p) * math.log2(1 - p))


# --------------------------------------------------------------------------- #
# registration: (name, mono, per-seq fn)
# --------------------------------------------------------------------------- #
_TEMPORAL = [
    ("dpd_acceleration_24m",        +1, _seq_acceleration,
     "Trajectory: recent-half DPD slope minus older-half slope (is lateness accelerating?)."),
    ("dpd_recent_minus_old_24m",    +1, _seq_recent_minus_old,
     "Trajectory: mean DPD of the recent half minus the older half (recent deterioration)."),
    ("recency_weighted_dpd_24m",    +1, _seq_recency_weighted,
     "Recency: exponentially recency-weighted mean DPD (how late LATELY, not on average)."),
    ("frac_late_recent_third_24m",  +1, _seq_frac_late_recent_third,
     "Recency: share of late payments in the most recent third of history."),
    ("current_clean_streak_24m",    -1, _seq_trailing_clean,
     "Recency: consecutive on-time payments at the END of the sequence (protective)."),
    ("dpd_late_autocorr_lag1_24m",  +1, lambda s: _seq_autocorr(s, 1),
     "Persistence: lag-1 autocorrelation of the late-payment indicator (lateness clustering)."),
    ("dpd_late_autocorr_lag2_24m",  +1, lambda s: _seq_autocorr(s, 2),
     "Persistence: lag-2 autocorrelation of the late-payment indicator."),
    ("dpd_late_autocorr_lag3_24m",  +1, lambda s: _seq_autocorr(s, 3),
     "Persistence: lag-3 autocorrelation of the late-payment indicator."),
    ("dpd_mean_abs_change_24m",     +1, _seq_mean_abs_change,
     "Volatility: mean absolute successive change in DPD (erratic vs steady lateness)."),
    ("late_payment_entropy_24m",     0, _seq_late_entropy,
     "Volatility: Shannon entropy of the on-time/late indicator (behavioural unpredictability)."),
]


def _make(seq_fn: Callable) -> Callable:
    def _f(ctx):
        return _agg_over_obligations(ctx, seq_fn)
    return _f


def _register_temporal() -> int:
    n = 0
    for name, mono, seq_fn, desc in _TEMPORAL:
        feature(name, "delinquency", mono, list(_DPD_COLS), parity=True,
                description=f"[Tier 2 temporal] {desc}")(_make(seq_fn))
        n += 1
    return n


N_TEMPORAL_FEATURES = _register_temporal()
