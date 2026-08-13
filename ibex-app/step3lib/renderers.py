"""Render ONE ground truth two ways.

  to_kaggle_frames(pop)      -> dict of pandas frames in the real Home Credit
                                column shape (what KaggleAdapter consumes).
  to_truelayer_payloads(pop) -> list of TrueLayer-style JSON payloads (what
                                TrueLayerAdapter consumes), i.e. the exact
                                format we would inherit from open banking.

Both are driven by the SAME GTApplicant objects, so the ONLY thing that can make
the two feature matrices differ is open banking's DPD-from-timing limitation --
which is precisely what Step 3 measures. Everything else (amounts, income,
affordability) is identical by construction.

Column names mirror obcredit/adapters/kaggle_adapter.py and the TrueLayer payload
shape documented in obcredit/adapters/truelayer_adapter.py.
"""
from __future__ import annotations
from datetime import timedelta
from typing import Dict, List

import pandas as pd

from .ground_truth import GTApplicant

# A payment later than this (days) cannot be told apart from a missed direct
# debit in an open-banking feed: it is beyond the schedule model's recovery
# window, so we render it as an ABSENT transaction (the adapter then imputes a
# capped DPD + overdue). This is the genuine open-banking limitation, and it
# matches what PaymentScheduleModel does with a large gap anyway.
OB_MISSED_DPD = 15


def _ob_payee(idx: int) -> str:
    """Distinct, LETTERS-ONLY payee name for the obligation at position `idx`.

    CRITICAL: the counterparty normaliser (_norm_counterparty) strips every
    digit before keying a recurring stream. An index encoded with digits (the
    old ``OBLIG <case>-<idx>`` name) therefore collapsed EVERY obligation of an
    applicant to the SAME key, so the recurring-stream detector merged all of a
    person's credit lines into a single open-banking stream. That destroyed
    num_active_obligations / total_annuity and jumbled the per-obligation DPD
    schedule. Encoding the index as an all-letter base-26 token keeps each line
    a distinct payee, exactly as separate lenders appear on a real statement.
    """
    n = idx + 1
    tag = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        tag = chr(65 + r) + tag
    return f"LOAN {tag}"


# --------------------------------------------------------------------------- #
# Kaggle bureau shape (delinquency read DIRECTLY from reported columns)
# --------------------------------------------------------------------------- #
def to_kaggle_frames(pop: List[GTApplicant]) -> Dict[str, pd.DataFrame]:
    base, cb, ap, static, card, person = [], [], [], [], [], []
    for app in pop:
        base.append({"case_id": app.case_id, "date_decision": app.as_of.isoformat()})
        static.append({"case_id": app.case_id, "maininc_215A": app.monthly_income})
        _d = getattr(app, "declared", {}) or {}
        person.append({"case_id": app.case_id, "num_group1": 0,
                       "mainoccupationinc_384A": _d.get("stated_income"),
                       "incometype_1044T": _d.get("income_type"),
                       "education_927M": _d.get("education"),
                       "housetype_905L": _d.get("housing"),
                       "empl_employedtotal_800L": _d.get("employment")})
        if app.balances:
            card.append({"case_id": app.case_id,
                         "last180dayaveragebalance_704A": app.balances[-1][1]})
        for g1, ob in enumerate(app.obligations):
            ap.append({"case_id": app.case_id, "num_group1": g1,
                       "annuity_853A": ob.instalment,
                       "credamount_770A": ob.instalment * ob.n_payments,
                       "creationdate_885D": ob.start.isoformat()})
            for g2, (i, due, dpd, overdue) in enumerate(ob.cycle_facts()):
                # The bureau reports every scheduled cycle. DPD + overdue are the
                # reported columns read directly; the monthly date only places
                # the payment in the 24m window.
                cb.append({"case_id": app.case_id, "num_group1": g1, "num_group2": g2,
                           "pmts_date_1107D": due.isoformat(),
                           "pmts_dpd_1073P": float(dpd),
                           "pmts_overdue_1140A": float(overdue)})
    return {
        "base": pd.DataFrame(base),
        "credit_bureau_a_2": pd.DataFrame(cb),
        "applprev_1": pd.DataFrame(ap),
        "static_0": pd.DataFrame(static),
        "debitcard_1": pd.DataFrame(card),
        "person_1": pd.DataFrame(person),
    }


# --------------------------------------------------------------------------- #
# Open-banking (TrueLayer) shape (delinquency RECONSTRUCTED from txn timing)
# --------------------------------------------------------------------------- #
_NOISE = [("TESCO", 23.50, 4), ("AMAZON", 64.00, 41), ("SHELL", 38.10, 63),
          ("ARGOS", 119.00, 102), ("UBER", 14.30, 130)]


def to_truelayer_payloads(pop: List[GTApplicant]) -> List[dict]:
    payloads = []
    for app in pop:
        txns: List[dict] = []
        # obligation repayments -> NEGATIVE amounts (money leaving the account).
        # A payment within the recovery window lands `dpd` days after its due
        # date (the schedule model recovers that lateness). A payment beyond the
        # window -- or a truly missed cycle -- is simply ABSENT.
        for idx, ob in enumerate(app.obligations):
            # Each obligation gets a DISTINCT, letters-only payee so the digit-
            # stripping counterparty normaliser cannot collapse an applicant's
            # separate credit lines into a single recurring stream. Using
            # ob.name here (e.g. "OBLIG 8001-0") was the BUILD 8 bug: the digits
            # were stripped and every line merged into one obligation.
            payee = _ob_payee(idx)
            for (i, due, dpd, overdue) in ob.cycle_facts():
                if dpd >= OB_MISSED_DPD:
                    continue  # indistinguishable from a missed direct debit
                pay = due + timedelta(days=int(dpd))
                txns.append({
                    "timestamp": pay.isoformat() + "T09:00:00Z",
                    "amount": -float(ob.instalment),
                    "description": f"DD {payee}",
                    "merchant_name": payee,
                    "transaction_category": "DIRECT_DEBIT",
                })
        # salary inflows -> POSITIVE, monthly, ending at the decision date
        if app.monthly_income and app.monthly_income > 0:
            for m in range(14):
                pay_day = app.as_of - timedelta(days=30 * (13 - m))
                txns.append({
                    "timestamp": pay_day.isoformat() + "T00:00:00Z",
                    "amount": float(app.monthly_income),
                    "description": "ACME CORP SALARY",
                    "merchant_name": "ACME CORP",
                    "transaction_category": "CREDIT",
                })
        # NOISE the detector must ignore (irregular one-off spend)
        for nm, amt, off in _NOISE:
            d = app.as_of - timedelta(days=off)
            txns.append({
                "timestamp": d.isoformat() + "T12:00:00Z",
                "amount": -amt, "description": f"POS {nm}",
                "merchant_name": nm, "transaction_category": "PURCHASE",
            })
        # direct-debit mandates: real open banking exposes the recurring mandate
        # list, so a credit line is KNOWN (payee + scheduled amount) even when too
        # few of its collections clear to form a >=3-payment detectable stream.
        # This is the faithful fix for the sub-3-payment reconstruction floor.
        direct_debits = [{
            "name": f"DD {_ob_payee(idx)}",
            "merchant_name": _ob_payee(idx),
            "amount": float(ob.instalment),
            "previous_payment_amount": float(ob.instalment),
            "transaction_category": "DIRECT_DEBIT",
            "status": "active",
        } for idx, ob in enumerate(app.obligations)]
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
                "direct_debits": direct_debits, "standing_orders": [],
            }],
            "declared": dict(getattr(app, "declared", {}) or {}),
        })
    return payloads
