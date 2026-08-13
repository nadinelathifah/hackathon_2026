"""Tier 3 -- Markov / regime-transition features (parity-safe).

WHY THIS MODULE EXISTS
----------------------
The Tier 1 aggregations summarise the LEVEL of the DPD stream; Tier 2
(feature_temporal.py) summarises its SHAPE over time (trend, recency,
persistence, volatility). Neither describes the stream as a STATE MACHINE.

A delinquency history is naturally a walk over arrears states:

    0 = current      (dpd <= 0)
    1 = mild         (1..29 dpd)
    2 = moderate     (30..89 dpd)
    3 = severe       (90+ dpd)      <- Basel CRR Art 178 default trigger zone

The risk question a bureau analyst actually asks is not "how late on average"
but "given that this person slipped, do they cure or do they deteriorate?".
That is a TRANSITION probability, and it is information the level and shape
features cannot express: two applicants can share an identical mean DPD, an
identical trend and an identical autocorrelation while one cures every slip
next cycle and the other rolls forward to 90+.

This is the standard roll-rate / transition-matrix framing used in provisioning
and collections analytics, and it is the closest thing in this pipeline to the
regime-switching structure the dissertation cites as future work.

PARITY DISCIPLINE (why these are parity=True)
---------------------------------------------
Identical discipline to Tier 2, and for the same reason. Every feature is
computed PER OBLIGATION on that obligation's date-ordered DPD sequence
(CanonicalObligation.dpd_values()), then averaged across obligations. The
state is a pure function of the DPD VALUE, and transitions are taken between
CONSECUTIVE POSITIONS IN THE SEQUENCE -- the sequence's own index, never an
absolute calendar date. The DPD values reconstruct elementwise-identically from
Kaggle and TrueLayer; the absolute payment dates do not (the bureau uses a
monthly grid). Index-based transitions -> parity-safe to 1e-6.

CUTPOINT PROVENANCE
-------------------
The 1/30/90 cutpoints are not tuned. They are the standard bureau arrears
buckets and 90+ is the regulatory default definition (CRR Art 178). Tuning
them on the training data would make the features data-dependent and would
require folding them into the selection funnel as hyperparameters; fixing them
at the regulatory grid keeps them auditable.

All features are defensive: they return None when the sequence is too short or
the conditioning state never occurs, so the applicant-level mean simply skips
that obligation (0.0 if no obligation qualifies), matching the Tier 2 pattern.
Duck-typed on FeatureContext (uses ctx.obligations_in_window, ctx.cfg).
"""
from __future__ import annotations
import statistics
from typing import Callable, List, Optional

from .feature_registry import feature

_DPD_COLS = ["pmts_dpd_1073P", "pmts_dpdvalue_108P"]

# arrears state cutpoints (days past due). Fixed at the regulatory grid.
_MILD = 1.0
_MODERATE = 30.0
_SEVERE = 90.0


# --------------------------------------------------------------------------- #
# infrastructure (mirrors feature_temporal.py exactly)
# --------------------------------------------------------------------------- #
def _window(ctx):
    return ctx.obligations_in_window(ctx.cfg.default_window_months)


def _agg_over_obligations(ctx, seq_fn: Callable[[List[float]], Optional[float]]) -> float:
    """Apply a per-sequence function to each obligation's DPD sequence and
    average the non-None results across obligations (0.0 if none qualify)."""
    vals: List[float] = []
    for o in _window(ctx):
        r = seq_fn(o.dpd_values())
        if r is not None:
            vals.append(float(r))
    return float(statistics.fmean(vals)) if vals else 0.0


def _state(v: float) -> int:
    """Map a DPD value onto an arrears state (0 current .. 3 severe)."""
    if v < _MILD:
        return 0
    if v < _MODERATE:
        return 1
    if v < _SEVERE:
        return 2
    return 3


def _states(seq: List[float]) -> List[int]:
    return [_state(float(v)) for v in seq]


def _transition_rate(seq: List[float],
                     from_ok: Callable[[int], bool],
                     to_ok: Callable[[int, int], bool]) -> Optional[float]:
    """Empirical transition probability over consecutive positions.

    Restricts to pairs whose ORIGIN state satisfies from_ok, then returns the
    fraction of those whose (origin, destination) pair satisfies to_ok. This is
    one row of the empirical transition matrix, conditioned as required.
    None if the sequence is too short or the origin state never occurs (an
    applicant who is never late has no late-to-cure probability to estimate --
    that is genuinely missing, not zero, and native NaN handling in LightGBM
    will learn the direction).
    """
    st = _states(seq)
    if len(st) < 2:
        return None
    pairs = [(st[i - 1], st[i]) for i in range(1, len(st))]
    elig = [(a, b) for a, b in pairs if from_ok(a)]
    if not elig:
        return None
    return sum(1.0 for a, b in elig if to_ok(a, b)) / len(elig)


# --------------------------------------------------------------------------- #
# per-sequence primitives
# --------------------------------------------------------------------------- #
def _seq_stay_current(seq: List[float]) -> Optional[float]:
    """P(current -> current): probability of staying on time given on time."""
    return _transition_rate(seq, lambda a: a == 0, lambda a, b: b == 0)


def _seq_current_to_late(seq: List[float]) -> Optional[float]:
    """P(current -> any arrears): the slip hazard from a clean state."""
    return _transition_rate(seq, lambda a: a == 0, lambda a, b: b > 0)


def _seq_late_to_cure(seq: List[float]) -> Optional[float]:
    """P(arrears -> current): the cure rate. Protective."""
    return _transition_rate(seq, lambda a: a > 0, lambda a, b: b == 0)


def _seq_late_to_worse(seq: List[float]) -> Optional[float]:
    """P(arrears -> deeper arrears): the roll-forward rate."""
    return _transition_rate(seq, lambda a: a > 0, lambda a, b: b > a)


def _seq_severe_persist(seq: List[float]) -> Optional[float]:
    """P(state >= moderate -> state >= moderate): how absorbing deep arrears
    is for this obligation. Approaches 1 for a genuinely defaulted line."""
    return _transition_rate(seq, lambda a: a >= 2, lambda a, b: b >= 2)


def _seq_mean_arrears_run(seq: List[float]) -> Optional[float]:
    """Expected time-to-cure: mean length (in payment cycles) of maximal runs
    of consecutive arrears states. Runs still open at the end of the sequence
    are right-censored and included at their observed length, which biases this
    DOWNWARD for currently-delinquent lines -- a conservative direction.
    None if the applicant was never late on this obligation."""
    st = _states(seq)
    runs: List[int] = []
    cur = 0
    for s in st:
        if s > 0:
            cur += 1
        elif cur:
            runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)
    if not runs:
        return None
    return float(statistics.fmean(runs))


# --------------------------------------------------------------------------- #
# registration: (name, monotonic, per-seq fn, description)
# --------------------------------------------------------------------------- #
_MARKOV = [
    ("mk_p_stay_current_24m",   -1, _seq_stay_current,
     "P(current -> current): persistence of good standing (protective)."),
    ("mk_p_current_to_late_24m", +1, _seq_current_to_late,
     "P(current -> arrears): slip hazard out of a clean state."),
    ("mk_p_late_to_cure_24m",   -1, _seq_late_to_cure,
     "P(arrears -> current): cure rate given a slip (protective)."),
    ("mk_p_late_to_worse_24m",  +1, _seq_late_to_worse,
     "P(arrears -> deeper arrears): roll-forward rate given a slip."),
    ("mk_p_severe_persist_24m", +1, _seq_severe_persist,
     "P(>=30dpd -> >=30dpd): how absorbing deep arrears is."),
    ("mk_mean_arrears_run_24m", +1, _seq_mean_arrears_run,
     "Expected time-to-cure: mean length of consecutive-arrears runs (cycles)."),
]


def _make(seq_fn: Callable) -> Callable:
    def _f(ctx):
        return _agg_over_obligations(ctx, seq_fn)
    return _f


def _register_markov() -> int:
    n = 0
    for name, mono, seq_fn, desc in _MARKOV:
        feature(name, "delinquency", mono, list(_DPD_COLS), parity=True,
                description=f"[Tier 3 markov] {desc}")(_make(seq_fn))
        n += 1
    return n


N_MARKOV_FEATURES = _register_markov()
