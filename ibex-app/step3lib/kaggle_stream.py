"""Turn REAL Kaggle Home Credit data into open-banking-shaped ground truth.

The heavy lifting (memory-safe streaming of the 188M-row bureau table into a
small per-case delinquency summary) reuses the EXACT aggregation that
scripts/run_logreg.py already proved on the full download. This module's own job
is the pure, unit-testable step:

    per-case delinquency summary  ->  GTApplicant

i.e. it reverse-engineers a plausible payment history whose reported figures
reproduce the case's summary, so we can then render it into the two shapes and
reconstruct features through the shared engine.

Modelling choice (documented, defensible):
  * serious arrears (a cycle at/over the 30-DPD threshold) -> a MISSED cycle,
    i.e. a failed direct debit. On open banking this is an ABSENT transaction,
    which the adapter imputes to a capped (90) DPD + an overdue = instalment.
    This is the robust open-banking signature of serious delinquency.
  * mild lateness (0 < DPD < 30) -> a payment that lands a few days late. The
    shared schedule model recovers small lateness directly from timing.
  * everything else -> on time.
Both capped at 90 (Basel), identically to the Kaggle side.

We cap the rendered history at 22 monthly cycles so every payment stays inside
the 24-month feature window; when a real case has more bureau payments we scale
its event counts down proportionally (documented approximation).
"""
from __future__ import annotations
from datetime import date, datetime, timedelta
from statistics import median
from typing import Dict, List, Optional, Tuple

import math

import pandas as pd

from .ground_truth import GTApplicant, GTObligation, DPD_CAP

MAX_CYCLES = 22          # keep rendered history inside the 24-month window
SERIOUS_DPD = 30.0
MILD_DPD_DAYS = 8        # small lateness the schedule model recovers (<15d)
DEFAULT_INSTALMENT = 100.0

# Summary columns produced by scripts/run_logreg.assemble_features (+ base join).
SUMMARY_COLS = [
    "max_dpd", "mean_dpd", "num_dpd_gt0", "num_dpd_ge30",
    "max_overdue", "total_overdue", "num_bureau_payments",
    "total_annuity", "max_annuity", "num_prev_apps", "monthly_income",
]


def _as_date(v) -> date:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    try:
        return pd.to_datetime(v).date()
    except Exception:
        return date(2020, 1, 1)


def _int(v, default: int = 0) -> int:
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return default
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


def _num(v, default: float = 0.0) -> float:
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def synthesize_applicant(case_id: str, row: dict) -> GTApplicant:
    """Build one GTApplicant from a per-case summary dict (pure; no I/O).

    `row` keys: the SUMMARY_COLS plus optional 'as_of'/'date_decision',
    'target', 'week'. Missing values default to 0 / no-signal.
    """
    as_of = _as_date(row.get("as_of") or row.get("date_decision") or date(2020, 1, 1))
    income = _num(row.get("monthly_income"), 0.0)
    n_pmts_real = _int(row.get("num_bureau_payments"), 0)

    obligations: List[GTObligation] = []
    if n_pmts_real > 0:
        n = min(n_pmts_real, MAX_CYCLES)
        scale = n / n_pmts_real if n_pmts_real > n else 1.0
        ge30 = min(_int(row.get("num_dpd_ge30") * scale if row.get("num_dpd_ge30") is not None else 0), n)
        gt0 = min(_int(row.get("num_dpd_gt0") * scale if row.get("num_dpd_gt0") is not None else 0), n)
        missed = ge30                              # serious arrears -> failed DDs
        # keep at least 3 present payments so the recurring-stream detector fires
        if n - missed < 3:
            missed = max(0, n - 3)
        mild = max(0, min(gt0 - ge30, n - missed))  # mild lateness (paid late)

        instalment = _num(row.get("max_annuity"), 0.0)
        if instalment <= 0:
            total_ann = _num(row.get("total_annuity"), 0.0)
            n_apps = _int(row.get("num_prev_apps"), 0)
            instalment = (total_ann / n_apps) if (total_ann > 0 and n_apps > 0) else DEFAULT_INSTALMENT

        start = as_of - timedelta(days=30 * n)
        missed_idx = set()
        # spread missed cycles across the (older) part of the history
        for j in range(missed):
            missed_idx.add(j * 2 if j * 2 < n else (n - 1 - j))
        late = {}
        max_mild = min(_num(row.get("max_dpd"), 0.0), MILD_DPD_DAYS) or MILD_DPD_DAYS
        placed = 0
        for i in range(n):
            if placed >= mild:
                break
            if i in missed_idx:
                continue
            late[i] = int(max_mild if placed == 0 else max(3, MILD_DPD_DAYS - 2))
            placed += 1

        obligations.append(GTObligation(
            name=f"LOAN {case_id}", instalment=round(instalment, 2), start=start,
            n_payments=n, period_days=30,
            missed=tuple(sorted(missed_idx)), late=late, kind="loan"))

    bal = None
    balances = []
    if bal is not None:
        balances = [(as_of, bal)]

    return GTApplicant(
        case_id=str(case_id), as_of=as_of, monthly_income=income,
        obligations=obligations, balances=balances,
        target=_int(row.get("target"), 0), week=_int(row.get("week"), 0),
    )


def summarize_to_applicants(summary: pd.DataFrame) -> List[GTApplicant]:
    """Vectorised wrapper: one GTApplicant per row of a per-case summary frame
    (indexed by case_id). Pure -- safe to unit-test with in-memory frames."""
    out: List[GTApplicant] = []
    for case_id, row in summary.iterrows():
        out.append(synthesize_applicant(str(case_id), row.to_dict()))
    return out


# --------------------------------------------------------------------------- #
# REAL per-payment path (no bucketing) -- preferred
# --------------------------------------------------------------------------- #
# Build ground truth straight from the real CanonicalApplicant produced by
# KaggleAdapter.stream_canonical(). Every scheduled bureau payment carries its
# OWN reported DPD + overdue verbatim (via GTObligation.dpd_seq/overdue_seq), so
# there is NO summary-level bucketing. The only source-difference that survives
# the round-trip is open banking's DPD-from-timing limit, applied in the
# renderer -- exactly what we want to measure.

def canonical_to_ground_truth(applicant, target: int = 0, week: int = 0,
                              default_instalment: float = DEFAULT_INSTALMENT,
                              max_cycles: int = MAX_CYCLES) -> GTApplicant:
    """Convert ONE real CanonicalApplicant into a GTApplicant with real per-cycle
    DPD/overdue sequences (no bucketing).

    The monthly instalment is a single representative value (median of the
    applicant's applprev annuities) applied to every obligation, so affordability
    renders parity-consistently on both sides; delinquency carries the true
    per-payment reported figures.
    """
    insts = [float(x) for x in (getattr(applicant, "instalments", None) or [])
             if x is not None and float(x) > 0]
    rep = float(median(insts)) if insts else float(default_instalment)
    as_of = applicant.as_of

    obligations: List[GTObligation] = []
    for oi, ob in enumerate(applicant.obligations):
        pays = sorted(getattr(ob, "payments", []), key=lambda p: p.date)
        if not pays:
            continue
        pays = pays[-max_cycles:]                    # keep history in the 24m window
        n = len(pays)
        dpd_seq = tuple(min(max(0.0, float(p.dpd or 0.0)), DPD_CAP) for p in pays)
        ovd_seq = tuple(max(0.0, float(p.overdue or 0.0)) for p in pays)
        start = as_of - timedelta(days=30 * n)
        obligations.append(GTObligation(
            name=f"OBLIG {applicant.case_id}-{oi}", instalment=round(rep, 2),
            start=start, n_payments=n, period_days=30,
            dpd_seq=dpd_seq, overdue_seq=ovd_seq,
            kind=getattr(ob, "kind", "loan") or "loan"))

    return GTApplicant(
        case_id=str(applicant.case_id), as_of=as_of,
        monthly_income=float(applicant.monthly_income or 0.0),
        obligations=obligations, balances=[],
        target=int(target), week=int(week),
        declared=dict(getattr(applicant, "declared", {}) or {}))


def canonical_pop_to_ground_truth(applicants,
                                  labels: Optional[Dict[str, Tuple[int, int]]] = None
                                  ) -> List[GTApplicant]:
    """Map an iterable of real CanonicalApplicants to GTApplicants.

    labels: optional case_id -> (target, week) from the base table.
    """
    labels = labels or {}
    out: List[GTApplicant] = []
    for a in applicants:
        t, w = labels.get(str(a.case_id), (0, 0))
        out.append(canonical_to_ground_truth(a, target=t, week=w))
    return out
