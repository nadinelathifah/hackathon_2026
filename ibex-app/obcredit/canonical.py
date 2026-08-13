"""Canonical schema -- the single source of truth that f() operates on.

Nothing in here knows about Kaggle or TrueLayer. Adapters are responsible for
mapping their raw shapes into these objects. Because both adapters target the
same objects, the feature library downstream is provably source-agnostic.

Units & conventions (documented because a bank reviewer will ask):
- All money is a positive float in account currency.
- For an OBLIGATION repayment, `amount` is the cash that LEFT the customer
  (a debit). Inflows are never stored as obligation payments.
- All dates are `datetime.date`.
- `as_of` is the decision date: NO data after this point may be used to build
  features (prevents target leakage).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, List, Optional, Tuple
import statistics


@dataclass
class CanonicalPayment:
    """One observed repayment against a single obligation.

    `overdue` is the unpaid portion of the scheduled instalment at this payment
    (the arrears). Bureau data provides it directly (pmts_overdue_1140A); open
    banking leaves it None and it is derived as max(0, instalment - amount).

    `dpd` is the days-past-due for this scheduled payment (0 = on time). It is
    the CORE delinquency primitive. On the Kaggle side it is read directly from
    the bureau (pmts_dpd_1073P / pmts_dpdvalue_108P); on open banking the
    schedule model measures lateness from transaction timing. Adapters store it
    already floored at 0 and capped (see EngineConfig.dpd_clip_days)."""
    obligation_id: str
    date: date
    amount: float
    overdue: Optional[float] = None
    dpd: Optional[float] = None


@dataclass
class CanonicalObligation:
    """A recurring credit commitment (loan / card / BNPL) and its payments."""
    obligation_id: str
    kind: str = "unknown"                 # 'loan' | 'card' | 'bnpl' | 'unknown'
    opened: Optional[date] = None
    credit_limit: Optional[float] = None
    payments: List[CanonicalPayment] = field(default_factory=list)

    def payment_dates(self) -> List[date]:
        return sorted(p.date for p in self.payments)

    def payment_amounts(self) -> List[float]:
        return [p.amount for p in sorted(self.payments, key=lambda x: x.date)]

    def scheduled_instalment(self) -> Optional[float]:
        """Robust estimate of the contractual instalment = median POSITIVE paid
        amount. Median (not mean) so a single double- or part-payment cannot move
        it; positive-only so synthetic zero-amount overdue events don't drag it."""
        amts = [a for a in self.payment_amounts() if a > 0]
        return float(statistics.median(amts)) if amts else None

    def overdue_amounts(self) -> List[float]:
        """Per-payment overdue amount, date-ordered. Uses the explicit overdue
        when the source provides it (Kaggle bureau); otherwise derives it from
        the cash shortfall vs the robust instalment (open banking)."""
        sched = self.scheduled_instalment()
        out: List[float] = []
        for p in sorted(self.payments, key=lambda x: x.date):
            if p.overdue is not None:
                out.append(max(0.0, float(p.overdue)))
            elif sched is not None:
                out.append(max(0.0, sched - p.amount))
            else:
                out.append(0.0)
        return out

    def dpd_values(self) -> List[float]:
        """Per-payment days-past-due, date-ordered. Reads the DPD stored on each
        payment (bureau-reported on the Kaggle side, schedule-inferred on the
        open-banking side); None -> 0.0 (on time). Adapters are responsible for
        flooring at 0 and capping, so the two sources are constructed alike."""
        return [max(0.0, float(p.dpd)) if p.dpd is not None else 0.0
                for p in sorted(self.payments, key=lambda x: x.date)]


@dataclass
class CanonicalAccount:
    """A deposit / current / card account, used for liquidity + income signals."""
    account_id: str
    type: str = "current"                 # 'current' | 'savings' | 'card'
    balances: List[Tuple[date, float]] = field(default_factory=list)  # (date, balance)


@dataclass
class CanonicalApplicant:
    """Everything known about ONE credit application at decision time."""
    case_id: str
    as_of: date
    obligations: List[CanonicalObligation] = field(default_factory=list)
    accounts: List[CanonicalAccount] = field(default_factory=list)
    monthly_income: Optional[float] = None
    # per-credit-line contractual monthly instalments (affordability features).
    # Kaggle: applprev annuity_780A per line; open banking: median paid per stream.
    instalments: List[float] = field(default_factory=list)
    declared: Dict[str, object] = field(default_factory=dict)

    # -- convenience accessors used by the feature layer --
    def all_payments(self) -> List[CanonicalPayment]:
        out: List[CanonicalPayment] = []
        for o in self.obligations:
            out.extend(o.payments)
        return sorted(out, key=lambda p: p.date)

    def balances_of_type(self, *types: str) -> List[Tuple[date, float]]:
        rows: List[Tuple[date, float]] = []
        for a in self.accounts:
            if a.type in types:
                rows.extend(a.balances)
        return sorted(rows, key=lambda x: x[0])
