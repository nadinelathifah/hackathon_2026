"""The shared feature library f()  --  days-past-due + overdue-amount edition.

Every function takes a FeatureContext (a thin cached wrapper over one
CanonicalApplicant) and returns a single float (or None). These functions are
the ONLY place features are defined and they run UNCHANGED on Kaggle-derived and
TrueLayer-derived canonical data: identical f() => identical construction =>
defensible train/inference parity.

THE CORE PRIMITIVE: DAYS-PAST-DUE (DPD)
---------------------------------------
Home Credit reports DPD explicitly, per scheduled payment, in the bureau tables
(credit_bureau_a_2.pmts_dpd_1073P, credit_bureau_b_2.pmts_dpdvalue_108P). The
Kaggle adapter reads it directly; the open-banking adapter reconstructs the same
quantity from transaction timing via the shared PaymentScheduleModel. Both floor
it at 0 and cap it at cfg.dpd_clip_days (Basel 90-DPD default; also tames the
bureau's outliers). Every DPD feature below is computed identically from
CanonicalObligation.dpd_values().

We ALSO keep the OVERDUE AMOUNT family: the unpaid instalment amount, which both
sources carry (Kaggle pmts_overdue_1140A; open banking imputes a missed direct
debit as an overdue of the instalment size). DPD captures *how late*; overdue
amount captures *how much* -- complementary, and both proven to carry signal on
the real data (single-column |Gini| ~0.27-0.32).

Families:
  A) Delinquency -- DPD          : how late payments are (core, parity-safe).
  A') Delinquency -- overdue amt : how much is in arrears (parity-safe).
  C) Exposure & affordability    : instalments vs income (parity-safe).
  D) Liquidity & income.
  B) Behavioural shape           : timing/amount; parity=False (the bureau grid
     cannot reproduce open-banking transaction timing).
"""
from __future__ import annotations
from datetime import date, timedelta
from typing import List, Optional
import hashlib
import statistics

from .canonical import CanonicalApplicant, CanonicalObligation
from .config import DEFAULT, EngineConfig
from .feature_registry import feature, REGISTRY
from .payment_engine import DelinquencyProfile, build_profile


class FeatureContext:
    """Caches the expensive intermediates so each feature is cheap & debuggable."""

    def __init__(self, applicant: CanonicalApplicant, cfg: EngineConfig = DEFAULT):
        self.a = applicant
        self.cfg = cfg
        self._profiles: Optional[List[DelinquencyProfile]] = None

    # ---- windowing helpers (enforce the as_of cutoff = no leakage) ----
    def _cutoff(self, months: int) -> date:
        return self.a.as_of - timedelta(days=int(30.44 * months))

    def obligations_in_window(self, months: int) -> List[CanonicalObligation]:
        lo = self._cutoff(months)
        out = []
        for o in self.a.obligations:
            pays = [p for p in o.payments if lo <= p.date <= self.a.as_of]
            if pays:
                out.append(CanonicalObligation(o.obligation_id, o.kind, o.opened,
                                               o.credit_limit, pays))
        return out

    def profiles(self, months: Optional[int] = None) -> List[DelinquencyProfile]:
        months = months or self.cfg.default_window_months
        return [build_profile(o, self.cfg) for o in self.obligations_in_window(months)]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _dpd_sequences(ctx: "FeatureContext") -> List[List[float]]:
    """Per-obligation list of per-payment DPD values (date-ordered, capped)."""
    return [o.dpd_values()
            for o in ctx.obligations_in_window(ctx.cfg.default_window_months)]


def _flat_dpd(ctx: "FeatureContext") -> List[float]:
    return [v for seq in _dpd_sequences(ctx) for v in seq]


def _overdue_sequences(ctx: "FeatureContext") -> List[List[float]]:
    """Per-obligation list of per-payment overdue amounts (date-ordered)."""
    return [o.overdue_amounts()
            for o in ctx.obligations_in_window(ctx.cfg.default_window_months)]


def _flat_overdue(ctx: "FeatureContext") -> List[float]:
    return [v for seq in _overdue_sequences(ctx) for v in seq]


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


def _longest_run(seqs: List[List[float]], pred) -> int:
    """Longest consecutive run within any single sequence where pred(v) holds."""
    best = 0
    for seq in seqs:
        cur = 0
        for v in seq:
            if pred(v):
                cur += 1
                best = max(best, cur)
            else:
                cur = 0
    return best


def _instalments(ctx: "FeatureContext") -> List[float]:
    """Per-line monthly instalments (parity-safe). Kaggle: applprev annuity;
    open banking: median paid per detected recurring obligation."""
    if ctx.a.instalments:
        return [float(x) for x in ctx.a.instalments if x]
    vals = [o.scheduled_instalment()
            for o in ctx.obligations_in_window(ctx.cfg.default_window_months)]
    return [float(v) for v in vals if v]


# =========================================================================== #
# A) DELINQUENCY  --  DAYS-PAST-DUE  (the core, parity-safe primitive)
# =========================================================================== #
@feature("max_dpd_24m", "delinquency", +1,
         ["pmts_dpd_1073P", "pmts_dpdvalue_108P", "dpdmax_139P"],
         description="Worst days-past-due across all obligations (24m, capped). "
                     "Kaggle: pmts_dpd_1073P; open banking: schedule-inferred lateness.")
def max_dpd_24m(ctx: FeatureContext):
    vals = _flat_dpd(ctx)
    return float(max(vals)) if vals else 0.0


@feature("mean_dpd_24m", "delinquency", +1,
         ["pmts_dpd_1073P", "pmts_dpdvalue_108P"],
         description="Mean days-past-due across observed payments (24m, capped).")
def mean_dpd_24m(ctx: FeatureContext):
    vals = _flat_dpd(ctx)
    return float(statistics.fmean(vals)) if vals else 0.0


@feature("num_dpd_events_24m", "delinquency", +1,
         ["pmts_dpd_1073P", "pmts_dpdvalue_108P"],
         description="Count of payments made late (DPD > 0) over 24m.")
def num_dpd_events_24m(ctx: FeatureContext):
    return float(sum(1 for v in _flat_dpd(ctx) if v > 0))


@feature("num_serious_arrears_24m", "delinquency", +1,
         ["pmts_dpd_1073P", "pmts_dpdvalue_108P", "numberofoverdueinstls_725L"],
         description="Count of payments in serious arrears (DPD >= threshold, default 30d).")
def num_serious_arrears_24m(ctx: FeatureContext):
    thr = ctx.cfg.dpd_serious_threshold
    return float(sum(1 for v in _flat_dpd(ctx) if v >= thr))


@feature("pct_dpd_payments_24m", "delinquency", +1,
         ["pmts_dpd_1073P", "pmts_dpdvalue_108P"],
         description="Share of payments made late (DPD > 0) over 24m.")
def pct_dpd_payments_24m(ctx: FeatureContext):
    vals = _flat_dpd(ctx)
    if not vals:
        return 0.0
    return float(sum(1 for v in vals if v > 0) / len(vals))


@feature("max_consecutive_dpd_24m", "delinquency", +1,
         ["pmts_dpd_1073P", "pmts_dpdvalue_108P"],
         description="Longest run of consecutive late payments within an obligation (24m).")
def max_consecutive_dpd_24m(ctx: FeatureContext):
    return float(_longest_run(_dpd_sequences(ctx), lambda v: v > 0))


@feature("dpd_trend_slope_24m", "delinquency", +1,
         ["pmts_dpd_1073P", "pmts_dpdvalue_108P"],
         description="Mean OLS slope of DPD over payment order: is lateness worsening?")
def dpd_trend_slope_24m(ctx: FeatureContext):
    slopes = [s for s in (_ols_slope(seq) for seq in _dpd_sequences(ctx)) if s is not None]
    return float(statistics.fmean(slopes)) if slopes else 0.0


@feature("longest_clean_streak_24m", "delinquency", -1,
         ["pmts_dpd_1073P", "pmts_dpdvalue_108P"],
         description="Longest run of consecutive on-time payments (DPD == 0; protective).")
def longest_clean_streak_24m(ctx: FeatureContext):
    return float(_longest_run(_dpd_sequences(ctx), lambda v: v <= 0))


# =========================================================================== #
# A') DELINQUENCY  --  OVERDUE AMOUNT  (how much is in arrears; parity-safe)
# =========================================================================== #
@feature("max_overdue_amount_24m", "delinquency", +1,
         ["pmts_overdue_1140A", "pmts_pmtsoverdue_635A", "overdueamountmax_155A"],
         description="Largest single overdue amount across all obligations (24m). "
                     "Kaggle: pmts_overdue_1140A; open banking: largest unpaid instalment.")
def max_overdue_amount_24m(ctx: FeatureContext):
    vals = _flat_overdue(ctx)
    return float(max(vals)) if vals else 0.0


@feature("avg_overdue_amount_24m", "delinquency", +1,
         ["pmts_overdue_1140A", "pmts_pmtsoverdue_635A"],
         description="Mean overdue amount across observed payments (24m).")
def avg_overdue_amount_24m(ctx: FeatureContext):
    vals = _flat_overdue(ctx)
    return float(statistics.fmean(vals)) if vals else 0.0


@feature("total_overdue_amount", "delinquency", +1,
         ["pmts_overdue_1140A", "pmts_pmtsoverdue_635A", "totaldebtoverduevalue_178A"],
         description="Sum of overdue amounts across obligations (24m).")
def total_overdue_amount(ctx: FeatureContext):
    return float(sum(_flat_overdue(ctx)))


@feature("num_overdue_payments_24m", "delinquency", +1,
         ["pmts_overdue_1140A", "pmts_pmtsoverdue_635A"],
         description="Count of payments in arrears (overdue > tol) over 24m.")
def num_overdue_payments_24m(ctx: FeatureContext):
    tol = ctx.cfg.overdue_amount_tol
    return float(sum(1 for v in _flat_overdue(ctx) if v > tol))


@feature("pct_overdue_payments_24m", "delinquency", +1,
         ["pmts_overdue_1140A", "pmts_pmtsoverdue_635A"],
         description="Share of payments in arrears over 24m.")
def pct_overdue_payments_24m(ctx: FeatureContext):
    vals = _flat_overdue(ctx)
    if not vals:
        return 0.0
    tol = ctx.cfg.overdue_amount_tol
    return float(sum(1 for v in vals if v > tol) / len(vals))


# =========================================================================== #
# C) EXPOSURE & AFFORDABILITY  (instalments vs income -- parity-safe)
# =========================================================================== #
@feature("num_active_obligations", "exposure", +1,
         ["numactivecreditcontr_374L", "numberofcontrsvalue_358L"],
         description="Number of distinct credit obligations active in window.")
def num_active_obligations(ctx: FeatureContext):
    return float(len(ctx.obligations_in_window(ctx.cfg.default_window_months)))


@feature("total_annuity", "exposure", +1,
         ["annuity_853A", "annuitynextmonth_57A"],
         description="Sum of monthly instalments across obligations.")
def total_annuity(ctx: FeatureContext):
    return float(sum(_instalments(ctx)))


@feature("avg_instalment_amount", "exposure", 0,
         ["annuity_853A"],
         description="Average instalment size across obligations.")
def avg_instalment_amount(ctx: FeatureContext):
    vals = _instalments(ctx)
    return float(statistics.fmean(vals)) if vals else 0.0


@feature("debt_to_income", "affordability", +1,
         ["annuity_853A", "maininc_215A"],
         description="Total monthly instalments / monthly income (None if no income).")
def debt_to_income(ctx: FeatureContext):
    inc = ctx.a.monthly_income
    if not inc:
        return None
    return float(sum(_instalments(ctx)) / inc)


@feature("payment_to_income", "affordability", +1,
         ["annuity_853A", "maininc_215A"],
         description="Largest single instalment / monthly income (None if no income).")
def payment_to_income(ctx: FeatureContext):
    inc = ctx.a.monthly_income
    if not inc:
        return None
    vals = _instalments(ctx)
    return float(max(vals) / inc) if vals else 0.0


# =========================================================================== #
# D) LIQUIDITY & INCOME
# =========================================================================== #
@feature("mean_balance_3m", "liquidity", -1,
         ["last180dayaveragebalance_704A", "avgoutstandbalancel6m_4187110A"],
         parity=False,
         description="Mean account balance over the short window (protective). "
                     "parity=False: Kaggle exposes only an aggregate balance column, "
                     "so an exact match with TrueLayer per-day balances is not expected.")
def mean_balance_3m(ctx: FeatureContext):
    lo = ctx.a.as_of - timedelta(days=int(30.44 * ctx.cfg.short_window_months))
    bals = [b for d, b in ctx.a.balances_of_type("current", "savings") if lo <= d <= ctx.a.as_of]
    return float(statistics.fmean(bals)) if bals else None


@feature("min_balance_3m", "liquidity", -1,
         ["last180dayaveragebalance_704A"],
         parity=False,
         description="Minimum account balance over the short window (buffer floor). "
                     "parity=False: Kaggle has only an aggregate balance column.")
def min_balance_3m(ctx: FeatureContext):
    lo = ctx.a.as_of - timedelta(days=int(30.44 * ctx.cfg.short_window_months))
    bals = [b for d, b in ctx.a.balances_of_type("current", "savings") if lo <= d <= ctx.a.as_of]
    return float(min(bals)) if bals else None


@feature("monthly_income", "income", -1,
         ["maininc_215A", "mainoccupationinc_437A"],
         description="Monthly income (detected salary inflow / declared).")
def monthly_income(ctx: FeatureContext):
    return float(ctx.a.monthly_income) if ctx.a.monthly_income else None


# =========================================================================== #
# B) BEHAVIOURAL SHAPE  (timing/amount; parity=False -- open-banking-only)
# =========================================================================== #
@feature("std_payment_interval_days", "alpha", +1, [], parity=False,
         description="Volatility of the payment cadence (SD of inter-payment gaps). "
                     "parity=False: the bureau monthly grid cannot reproduce "
                     "open-banking transaction timing.")
def std_payment_interval_days(ctx: FeatureContext):
    stds = [p.interval_std_days() for p in ctx.profiles()]
    stds = [s for s in stds if s is not None]
    return float(statistics.fmean(stds)) if stds else 0.0


@feature("cv_payment_amount", "alpha", +1, [], parity=False,
         description="Coefficient of variation of payment amounts (irregular paydowns). "
                     "parity=False: bureau rows carry overdue amounts, not cash paid.")
def cv_payment_amount(ctx: FeatureContext):
    cvs = []
    for o in ctx.obligations_in_window(ctx.cfg.default_window_months):
        amts = [a for a in o.payment_amounts() if a > 0]
        if len(amts) >= 2:
            mean = statistics.fmean(amts)
            if mean:
                cvs.append(statistics.pstdev(amts) / abs(mean))
    return float(statistics.fmean(cvs)) if cvs else 0.0


# =========================================================================== #
# E) DECLARED / ONBOARDING ATTRIBUTES  (application facts captured at onboarding
#    on BOTH sources -> parity-safe; raise the ceiling without new bureau data)
# =========================================================================== #
def _declared(ctx: "FeatureContext", key: str):
    d = getattr(ctx.a, "declared", None) or {}
    v = d.get(key)
    if v is None or v == "":
        return None
    return v


def _code(s) -> float:
    """Stable categorical -> numeric code (md5; process-independent unlike hash())."""
    return float(int(hashlib.md5(str(s).encode("utf-8")).hexdigest()[:6], 16))


@feature("declared_income_is_employment", "stability", -1,
         ["incometype_1044T"],
         description="1.0 if declared income type is employment/salary (protective).")
def declared_income_is_employment(ctx: FeatureContext):
    v = _declared(ctx, "income_type")
    if v is None:
        return None
    s = str(v).lower()
    hit = any(k in s for k in ("salar", "employ", "wage", "work", "private_sector"))
    return 1.0 if hit else 0.0


@feature("declared_is_homeowner", "stability", -1,
         ["housetype_905L"],
         description="1.0 if declared housing indicates home ownership (protective).")
def declared_is_homeowner(ctx: FeatureContext):
    v = _declared(ctx, "housing")
    if v is None:
        return None
    return 1.0 if "own" in str(v).lower() else 0.0


@feature("declared_income_type_code", "stability", 0,
         ["incometype_1044T"],
         description="Stable numeric code for the declared income type (tree split).")
def declared_income_type_code(ctx: FeatureContext):
    v = _declared(ctx, "income_type")
    return _code(v) if v is not None else None


@feature("declared_education_code", "stability", 0,
         ["education_927M"],
         description="Stable numeric code for the declared education level.")
def declared_education_code(ctx: FeatureContext):
    v = _declared(ctx, "education")
    return _code(v) if v is not None else None


@feature("declared_housing_code", "stability", 0,
         ["housetype_905L"],
         description="Stable numeric code for the declared housing tenure.")
def declared_housing_code(ctx: FeatureContext):
    v = _declared(ctx, "housing")
    return _code(v) if v is not None else None


@feature("declared_employment_code", "stability", 0,
         ["empl_employedtotal_800L"],
         description="Stable numeric code for the declared employment-tenure bucket.")
def declared_employment_code(ctx: FeatureContext):
    v = _declared(ctx, "employment")
    return _code(v) if v is not None else None


@feature("declared_income_gap", "affordability", +1,
         ["mainoccupationinc_384A", "maininc_215A"],
         description="|declared stated income - observed income| / max(...): "
                     "cross-verification of ability to pay (risk-increasing).")
def declared_income_gap(ctx: FeatureContext):
    stated = _declared(ctx, "stated_income")
    obs = ctx.a.monthly_income
    if not stated or not obs:
        return None
    stated = float(stated)
    obs = float(obs)
    denom = max(abs(stated), abs(obs))
    if denom <= 0:
        return None
    return float(abs(stated - obs) / denom)


def list_features():
    """Return all registered FeatureSpecs (import side-effect registers them)."""
    return REGISTRY.all()


# --------------------------------------------------------------------------- #
# Tier 1 aggregation grammar. Imported last so the base helpers above are all
# defined; importing registers the agg_* features as a side effect. Kept in a
# separate module so the hand-written features stay readable.
# --------------------------------------------------------------------------- #
from . import feature_aggregations as _feature_aggregations  # noqa: E402,F401

# --------------------------------------------------------------------------- #
# Tier 2 temporal / trajectory features (parity-safe). Importing registers the
# order/timing-aware DPD features that the Tier 1 aggregations discard.
# --------------------------------------------------------------------------- #
from . import feature_temporal as _feature_temporal  # noqa: E402,F401
from . import feature_recency as _feature_recency  # noqa: E402,F401  # BUILD 22 RECENCY
from . import feature_markov as _feature_markov    # noqa: E402,F401  # BUILD 22 MARKOV
