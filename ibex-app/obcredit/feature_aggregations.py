"""Tier 1 -- Depth-aware aggregation grammar (parity-safe breadth).

Systematically expands the high-fidelity per-payment streams into a family of
summary statistics, turning a handful of hand-written delinquency features into
a broad, model-ready matrix WITHOUT changing the reconstruction: every stream
here is built from the SAME CanonicalObligation payments that f() already uses,
so Kaggle-derived and TrueLayer-derived inputs still produce identical values
for the parity-safe streams.

Streams
-------
  dpd       per-payment days-past-due            (parity-safe -- core primitive)
  instal    per-line contractual instalment       (parity-safe -- annuity / median)
  interval  days between consecutive payments      (parity=False -- OB timing)

Operators (the "grammar")
-------------------------
  count, mean, max, min, std, sum, median, nonzero   (order-independent)
  first, last                                        (order-dependent)

Order-dependent operators are only emitted for streams whose element order is
itself parity-safe (a globally date-ordered stream). The per-line instalment
"stream" has no meaningful cross-source order, so first/last are skipped for it.

WHY interval is parity=False
----------------------------
The Kaggle bureau reports payments on a monthly grid, so inter-payment gaps are
a construct of open-banking transaction timing that the bureau cannot reproduce
exactly (same reason std_payment_interval_days is parity=False). These features
are still useful OB-native behavioural signal; they simply do not enter the
Kaggle-vs-OB parity comparison and are excluded from the parity-only XGBoost run.

Naming: every generated feature is prefixed `agg_<stream>_<op>` so it never
collides with the hand-written features and is trivial to spot in importance
tables and the selection funnel. Some are intentionally redundant with the
hand-written ones (e.g. agg_dpd_max == max_dpd_24m); the correlation-cluster
dedupe step in selection collapses those.
"""
from __future__ import annotations
import statistics
from typing import Callable, List

from .feature_registry import feature

_DPD_COLS = ("pmts_dpd_1073P", "pmts_dpdvalue_108P")
_ANNUITY_COLS = ("annuity_853A",)


# --------------------------------------------------------------------------- #
# stream extractors
# (duck-typed on FeatureContext: uses ctx.a, ctx.cfg, ctx.obligations_in_window)
# --------------------------------------------------------------------------- #
def _window(ctx):
    return ctx.obligations_in_window(ctx.cfg.default_window_months)


def _dpd_stream(ctx) -> List[float]:
    """Every payment's capped DPD, globally date-ordered across obligations.
    Order is parity-safe: both sources present payments on the same ascending
    monthly schedule, so first/last map to the same cycle."""
    pays = [p for o in _window(ctx) for p in o.payments]
    pays.sort(key=lambda p: p.date)
    return [max(0.0, float(p.dpd)) if p.dpd is not None else 0.0 for p in pays]


def _instal_stream(ctx) -> List[float]:
    """Per-line contractual instalment (parity-safe). Same logic as
    feature_functions._instalments: Kaggle applprev annuity, else the median
    positive paid per detected recurring obligation. No cross-source element
    order, so only order-independent operators are emitted for it."""
    if ctx.a.instalments:
        return [float(x) for x in ctx.a.instalments if x]
    vals = [o.scheduled_instalment() for o in _window(ctx)]
    return [float(v) for v in vals if v]


def _interval_stream(ctx) -> List[float]:
    """Days between consecutive payments within each obligation (OB timing)."""
    out: List[float] = []
    for o in _window(ctx):
        ds = sorted(p.date for p in o.payments)
        out.extend(float((b - a).days) for a, b in zip(ds, ds[1:]))
    return out


# --------------------------------------------------------------------------- #
# aggregation operators (all defensive on empty / short sequences)
# --------------------------------------------------------------------------- #
def _count(s):   return float(len(s))
def _mean(s):    return float(statistics.fmean(s)) if s else 0.0
def _max(s):     return float(max(s)) if s else 0.0
def _min(s):     return float(min(s)) if s else 0.0
def _std(s):     return float(statistics.pstdev(s)) if len(s) >= 2 else 0.0
def _sum(s):     return float(sum(s))
def _median(s):  return float(statistics.median(s)) if s else 0.0
def _nonzero(s): return float(sum(1 for v in s if v > 0.0))
def _first(s):   return float(s[0]) if s else 0.0
def _last(s):    return float(s[-1]) if s else 0.0


# (op_name, fn, needs_order, mono_if_risk_value)
_OPS = [
    ("count",   _count,   False, 0),
    ("mean",    _mean,    False, +1),
    ("max",     _max,     False, +1),
    ("min",     _min,     False, +1),
    ("std",     _std,     False, +1),
    ("sum",     _sum,     False, +1),
    ("median",  _median,  False, +1),
    ("nonzero", _nonzero, False, +1),
    ("first",   _first,   True,  +1),
    ("last",    _last,    True,  +1),
]

# (stream_name, stream_fn, family, kaggle_cols, parity, risk_value, ordered_ok)
_STREAMS = [
    ("dpd",      _dpd_stream,      "delinquency", _DPD_COLS,     True,  True,  True),
    ("instal",   _instal_stream,   "exposure",    _ANNUITY_COLS, True,  False, False),
    ("interval", _interval_stream, "alpha",       (),            False, False, False),
]


def _make(stream_fn: Callable, op_fn: Callable) -> Callable:
    def _f(ctx):
        return op_fn(stream_fn(ctx))
    return _f


def _register_grammar() -> int:
    """Register every (stream x operator) combination once. Returns the count."""
    n = 0
    for sname, sfn, family, kcols, parity, risk_value, ordered_ok in _STREAMS:
        for op_name, op_fn, needs_order, mono_flag in _OPS:
            if needs_order and not ordered_ok:
                continue
            name = f"agg_{sname}_{op_name}"
            mono = mono_flag if risk_value else 0
            tag = "parity-safe" if parity else "OB-native, parity=False"
            desc = (f"Aggregation grammar [Tier 1]: {op_name} of the {sname} "
                    f"per-payment stream over the 24m window ({tag}).")
            feature(name, family, mono, list(kcols), parity=parity,
                    description=desc)(_make(sfn, op_fn))
            n += 1
    return n


N_GRAMMAR_FEATURES = _register_grammar()
