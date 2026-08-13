"""Source-neutral ground truth -- one applicant's real payment history.

This is the SINGLE description of what happened for an applicant. We render it
two ways (Kaggle bureau shape, and open-banking transaction shape) so we can
prove the features are identical whichever way we construct them.

A cycle (one scheduled monthly payment) has a real days-past-due (DPD) and a real
overdue amount. There are two ways to specify a history:

  * REAL per-cycle sequence (`dpd_seq` / `overdue_seq`): used when we build the
    ground truth from actual Kaggle bureau rows -- NO bucketing, the true
    reported DPD of every scheduled payment is carried through verbatim.
  * synthetic shorthand (`missed` / `late`): used by the fixtures and unit tests
    to hand-write simple histories (a cycle is on-time, a few days late, or a
    missed/failed direct debit).

How a cycle appears to each source is decided by the renderers, not here:
  * Kaggle bureau : every scheduled cycle is a reported row; DPD + overdue are
    read directly (this is what the bureau literally provides).
  * Open banking  : a payment made within the schedule model's recovery window
    is observed late (DPD recovered from timing); a payment later than that, or
    never made, is indistinguishable from a missed direct debit and is rendered
    as an ABSENT transaction (the adapter imputes a capped DPD + overdue). This
    is the genuine, documented open-banking limitation the experiment measures.

All DPD is floored at 0 and capped at 90 (Basel default), identically on both
sides.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

DPD_CAP = 90.0

# One resolved cycle: (index, due date, real DPD, real overdue amount).
CycleFact = Tuple[int, date, float, float]


@dataclass
class GTObligation:
    """A recurring credit commitment and the fate of each of its cycles."""
    name: str                       # counterparty, e.g. "ACME LOAN"
    instalment: float               # contractual monthly instalment
    start: date
    n_payments: int
    period_days: int = 30
    # --- synthetic shorthand (fixtures/tests) ---
    missed: Tuple[int, ...] = ()    # cycle indices with NO payment (absent txn)
    late: Dict[int, int] = field(default_factory=dict)  # cycle index -> dpd days
    # --- real per-cycle sequences (built from actual bureau rows; no bucketing) ---
    dpd_seq: Optional[Tuple[float, ...]] = None
    overdue_seq: Optional[Tuple[float, ...]] = None
    kind: str = "loan"

    def cycle_facts(self) -> List[CycleFact]:
        """Resolve every cycle to (index, due date, real DPD, real overdue)."""
        out: List[CycleFact] = []
        if self.dpd_seq is not None:
            for i in range(self.n_payments):
                due = self.start + timedelta(days=i * self.period_days)
                dpd = float(min(max(0.0, self.dpd_seq[i]), DPD_CAP))
                if self.overdue_seq is not None:
                    ovd = float(max(0.0, self.overdue_seq[i]))
                else:
                    ovd = float(self.instalment) if dpd >= DPD_CAP else 0.0
                out.append((i, due, dpd, ovd))
            return out
        # synthetic shorthand
        missed = set(self.missed)
        for i in range(self.n_payments):
            due = self.start + timedelta(days=i * self.period_days)
            if i in missed:
                out.append((i, due, DPD_CAP, float(self.instalment)))
            else:
                dpd = float(min(int(self.late.get(i, 0)), int(DPD_CAP)))
                out.append((i, due, dpd, 0.0))
        return out

    def n_present(self) -> int:
        return self.n_payments - len(set(self.missed))


@dataclass
class GTApplicant:
    """Everything known about ONE application at decision time."""
    case_id: str
    as_of: date
    monthly_income: float
    obligations: List[GTObligation]
    balances: List[Tuple[date, float]] = field(default_factory=list)
    # optional supervised label + competition week, used by the modelling step
    target: int = 0
    week: int = 0
    # declared onboarding attributes (income_type, education, housing,
    # employment, stated_income); rendered verbatim to both sources.
    declared: Dict[str, object] = field(default_factory=dict)
