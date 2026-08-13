"""Generate a MATCHED scenario in both raw schemas from ONE ground truth.

This is the engine of the parity proof. We invent a small population of
applicants with explicit, known payment histories (the 'ground truth'). We then
RENDER that exact same truth two ways:

  * Kaggle shape  : credit_bureau_a_2 / applprev_1 / static_0 / base frames,
                    using the real Home Credit column names.
  * TrueLayer shape: accounts + transactions + balances JSON, with realistic
                    descriptions, the TrueLayer negative-outflow sign convention,
                    plus NOISE transactions (groceries, one-off purchases) that a
                    real feed contains and that the stream detector must ignore.

If the adapters + shared f() are correct, both renderings must yield identical
features. Any divergence is a real bug, not a data artefact -- that is exactly
what 'ultra defensible' requires.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Tuple

import pandas as pd

from obcredit.config import DEFAULT


# --------------------------------------------------------------------------- #
# ground-truth model (source-neutral)
# --------------------------------------------------------------------------- #
@dataclass
class GTObligation:
    name: str                 # human counterparty, e.g. "ACME LOAN"
    instalment: float         # contractual monthly instalment
    start: date
    n_payments: int
    period_days: int = 30
    # cycles with NO payment at all (a missed / failed direct debit). The bureau
    # still reports the cycle, with an overdue amount = the instalment; open
    # banking simply shows no transaction and the adapter imputes the overdue.
    # (Do not miss the LAST cycle -- a trailing gap is not observable either
    # side; that is a known, documented limitation.)
    missed: Tuple[int, ...] = ()
    kind: str = "loan"

    def events(self) -> List[Tuple[int, date, float, float, bool]]:
        """Per cycle: (index, date, paid_amount, overdue_amount, present)."""
        out = []
        for i in range(self.n_payments):
            due = self.start + timedelta(days=i * self.period_days)
            if i in self.missed:
                out.append((i, due, 0.0, self.instalment, False))
            else:
                out.append((i, due, self.instalment, 0.0, True))
        return out


@dataclass
class GTApplicant:
    case_id: str
    as_of: date
    monthly_income: float
    obligations: List[GTObligation]
    balances: List[Tuple[date, float]] = field(default_factory=list)
    declared: Dict[str, object] = field(default_factory=dict)


def default_population() -> List[GTApplicant]:
    """A small, deliberately varied population (clean payer, late payer, skipper)."""
    base = date(2025, 1, 6)
    pop = [
        GTApplicant(
            case_id="1001", as_of=date(2026, 1, 10), monthly_income=3200.0,
            obligations=[
                GTObligation("ACME LOAN", 250.0, base, 12, 30),        # spotless
                GTObligation("PHONE FINANCE", 45.0, base, 12, 30,
                             missed=(5,)),                             # one missed cycle
            ],
            balances=[(date(2025, 11, 15), 1800.0), (date(2025, 12, 15), 1500.0),
                      (date(2026, 1, 5), 2100.0)],
            declared={"income_type": "SALARIED", "education": "HIGHER_EDU",
                      "housing": "OWNED", "employment": "MORE_ONE_YEAR",
                      "stated_income": 3200.0},
        ),
        GTApplicant(
            case_id="1002", as_of=date(2026, 1, 10), monthly_income=2100.0,
            obligations=[
                GTObligation("KLARNA", 80.0, base, 12, 30,
                             missed=(3, 8), kind="bnpl"),
                GTObligation("CAR LOAN", 320.0, base, 12, 30, missed=(7,)),
            ],
            balances=[(date(2025, 11, 15), 300.0), (date(2025, 12, 15), 90.0),
                      (date(2026, 1, 5), 45.0)],
            declared={"income_type": "OTHER", "education": "SECONDARY",
                      "housing": "RENTED", "employment": "LESS_ONE_YEAR",
                      "stated_income": 2500.0},
        ),
    ]
    return pop


# --------------------------------------------------------------------------- #
# render -> Kaggle frames
# --------------------------------------------------------------------------- #
class KaggleRenderer:
    def render(self, pop: List[GTApplicant]) -> Dict[str, pd.DataFrame]:
        base_rows, cb_rows, ap_rows, static_rows, card_rows, person_rows = [], [], [], [], [], []
        for app in pop:
            base_rows.append({"case_id": app.case_id,
                              "date_decision": app.as_of.isoformat()})
            static_rows.append({"case_id": app.case_id, "maininc_215A": app.monthly_income})
            _d = getattr(app, "declared", {}) or {}
            person_rows.append({"case_id": app.case_id, "num_group1": 0,
                                "mainoccupationinc_384A": _d.get("stated_income"),
                                "incometype_1044T": _d.get("income_type"),
                                "education_927M": _d.get("education"),
                                "housetype_905L": _d.get("housing"),
                                "empl_employedtotal_800L": _d.get("employment")})
            if app.balances:
                # use the latest known balance as the 180d-average proxy column
                card_rows.append({"case_id": app.case_id,
                                  "last180dayaveragebalance_704A": app.balances[-1][1]})
            for g1, ob in enumerate(app.obligations):
                ap_rows.append({"case_id": app.case_id, "num_group1": g1,
                                "annuity_853A": ob.instalment,
                                "credamount_770A": ob.instalment * ob.n_payments,
                                "credacc_credlmt_575A": ob.instalment * ob.n_payments,
                                "creationdate_885D": ob.start.isoformat()})
                # bureau reports EVERY scheduled cycle on a monthly grid; the
                # delinquency is the overdue AMOUNT, not the date spacing.
                for g2, (i, d, paid, overdue, present) in enumerate(ob.events()):
                    # bureau-reported DPD: 0 when the cycle was paid on time, the
                    # capped missed-DPD when the cycle was skipped (mirrors how the
                    # open-banking adapter scores an absent direct debit).
                    dpd = 0.0 if present else float(DEFAULT.missed_dpd_cap_days)
                    cb_rows.append({"case_id": app.case_id, "num_group1": g1,
                                    "num_group2": g2,
                                    "pmts_date_1107D": d.isoformat(),
                                    "pmts_dpd_1073P": dpd,
                                    "pmts_overdue_1140A": overdue})
        return {
            "base": pd.DataFrame(base_rows),
            "credit_bureau_a_2": pd.DataFrame(cb_rows),
            "applprev_1": pd.DataFrame(ap_rows),
            "static_0": pd.DataFrame(static_rows),
            "debitcard_1": pd.DataFrame(card_rows),
            "person_1": pd.DataFrame(person_rows),
        }


# --------------------------------------------------------------------------- #
# render -> TrueLayer JSON (with noise)
# --------------------------------------------------------------------------- #
class TrueLayerRenderer:
    """Render the same truth as a TrueLayer payload, including decoy txns."""

    def render(self, pop: List[GTApplicant]) -> List[dict]:
        payloads = []
        for app in pop:
            txns: List[dict] = []
            # obligation repayments -> NEGATIVE amounts (money out), realistic desc
            for ob in app.obligations:
                for (i, d, paid, overdue, present) in ob.events():
                    if not present:
                        continue          # a missed DD is simply an ABSENT txn
                    txns.append({
                        "timestamp": d.isoformat() + "T09:00:00Z",
                        "amount": -paid,                       # outflow
                        "description": f"DD {ob.name}",
                        "merchant_name": ob.name,
                        "transaction_category": "DIRECT_DEBIT",
                    })
            # salary inflows -> POSITIVE, monthly
            for m in range(14):
                pay_day = date(2025, 1, 28) + timedelta(days=30 * m)
                if pay_day <= app.as_of:
                    txns.append({
                        "timestamp": pay_day.isoformat() + "T00:00:00Z",
                        "amount": app.monthly_income,
                        "description": "ACME CORP SALARY",
                        "merchant_name": "ACME CORP",
                        "transaction_category": "CREDIT",
                    })
            # NOISE: one-off / irregular spend the detector MUST ignore. Each
            # merchant appears with varying amounts and irregular spacing (and
            # most fewer than the 3-payment recurrence threshold), so none can
            # be mistaken for a recurring obligation.
            noise_specs = [
                ("TESCO", 23.50, 4), ("TESCO", 51.20, 27), ("AMAZON", 64.00, 41),
                ("SHELL", 38.10, 63), ("NETFLIX_ONEOFF", 9.99, 88),
                ("ARGOS", 119.00, 102), ("UBER", 14.30, 130),
            ]
            for nm, amt, day_off in noise_specs:
                d = date(2025, 2, 1) + timedelta(days=day_off)
                if d <= app.as_of:
                    txns.append({
                        "timestamp": d.isoformat() + "T12:00:00Z",
                        "amount": -amt,
                        "description": f"POS {nm}",
                        "merchant_name": nm,
                        "transaction_category": "PURCHASE",
                    })
            balances = [{"current": b, "timestamp": d.isoformat() + "T23:59:00Z"}
                        for d, b in app.balances]
            payloads.append({
                "case_id": app.case_id,
                "as_of": app.as_of.isoformat(),
                "accounts": [{
                    "account_id": f"acc-{app.case_id}",
                    "account_type": "TRANSACTION",
                    "transactions": txns,
                    "balances": balances,
                    "direct_debits": [], "standing_orders": [],
                }],
                "declared": dict(getattr(app, "declared", {}) or {}),
            })
        return payloads


def build_matched_fixtures():
    """Return (kaggle_frames, truelayer_payloads) from one shared ground truth."""
    pop = default_population()
    return KaggleRenderer().render(pop), TrueLayerRenderer().render(pop)
