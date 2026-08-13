"""BUILD 18 -- mock open-banking payload generator for the demo.

Produces a payload in the SAME shape TrueLayerDataClient.fetch_user() returns, so
the REAL TrueLayerAdapter reconstructs income + obligations from transactions --
i.e. the genuine open-banking path, just with synthetic transactions instead of a
live sandbox pull. To go live, replace make_payload(...) with a call to
obcredit.truelayer.client.TrueLayerDataClient(...).fetch_user(case_id, as_of)
against the uk-cs-mock sandbox account.

BUILD 18 changes
----------------
* Default history is now 24 months, not 12, matching what a real TrueLayer
  consented pull returns and giving the temporal (Tier-2) features a full two
  years of cycles to measure trend and volatility over.
* Weekly balance snapshots instead of one per month, so cashflow / minimum-
  balance features have something real to read.
* Richer, optional behaviour: savings account, overdraft episodes, gambling
  spend, returned-direct-debit (NSF) fees, 4-weekly pay cycles, annual bonus,
  and a secondary income stream.
* build_custom(spec) lets the UI drive every knob directly.

Sign convention (TrueLayer): amount NEGATIVE = money leaving the account. The
adapter flips this to the canonical positive-outflow convention.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

DEFAULT_MONTHS = 24


def _month_start(d: date, k: int) -> date:
    """First day of the month, shifted back k months from d's month."""
    y = d.year
    m = d.month - k
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def _ts(d: date) -> str:
    return datetime(d.year, d.month, d.day, 9, 0, 0).isoformat() + "Z"


def _txn(d: date, amount: float, desc: str, merchant: str, category: str) -> dict:
    return {
        "timestamp": _ts(d),
        "amount": round(float(amount), 2),
        "description": desc,
        "merchant_name": merchant,
        "transaction_category": category,
    }


def _day(m_start: date, day: int) -> date:
    """Safe day-of-month within m_start's month (never rolls over)."""
    return m_start.replace(day=max(1, min(int(day), 28)))


def make_payload(
    case_id: str,
    as_of: date,
    monthly_income: float,
    loan_instalment: float,
    card_payment: float,
    rent: float,
    declared: Dict[str, object],
    missed_cycles: Optional[List[int]] = None,
    late_cycles: Optional[Dict[int, int]] = None,
    opening_balance: float = 1500.0,
    months: int = DEFAULT_MONTHS,
    # ---- BUILD 18 optional richness (all default to the old behaviour) ----
    income_day: int = 25,
    income_freq: str = "monthly",          # "monthly" | "4weekly"
    secondary_income: float = 0.0,
    bonus_months: Optional[Dict[int, float]] = None,
    savings_balance: float = 0.0,
    savings_monthly: float = 0.0,
    gambling_monthly: float = 0.0,
    overdraft_cycles: Optional[Dict[int, float]] = None,
    nsf_cycles: Optional[List[int]] = None,
    grocery_monthly: float = 220.0,
    fuel_monthly: float = 85.0,
) -> dict:
    """Build a multi-month payload (24 months by default).

    missed_cycles    : month indices (0 = oldest) where the loan direct debit
                       does NOT clear -> arrears / days-past-due for that line.
    late_cycles      : {month_index: days_late} for loan payments clearing late.
    bonus_months     : {month_index: amount} extra one-off salary credits.
    overdraft_cycles : {month_index: amount} a mid-month debit large enough to
                       push the account into overdraft, repaid at month end.
    nsf_cycles       : month indices where a direct debit bounces and the bank
                       charges a returned-payment fee. Strong risk signal.
    """
    missed = set(missed_cycles or [])
    late = dict(late_cycles or {})
    bonus = dict(bonus_months or {})
    od = dict(overdraft_cycles or {})
    nsf = set(nsf_cycles or [])

    txns: List[dict] = []
    balance = float(opening_balance)
    balances: List[dict] = []

    def snap(d: date) -> None:
        balances.append({"current": round(balance, 2), "timestamp": _ts(d)})

    for i in range(months):
        m_start = _month_start(as_of, months - 1 - i)

        # --- income ---
        if income_freq == "4weekly":
            # four-weekly pay lands 13x/yr; approximate within the month as two
            # credits of half the monthly amount, 14 days apart.
            for k, dd in enumerate((income_day - 14, income_day)):
                pay_day = _day(m_start, dd if dd >= 1 else 1)
                amt = monthly_income / 2.0
                txns.append(_txn(pay_day, +amt, "ACME PAYROLL SALARY",
                                 "ACME PAYROLL", "TRANSFER"))
                balance += amt
        else:
            pay_day = _day(m_start, income_day)
            txns.append(_txn(pay_day, +monthly_income, "ACME PAYROLL SALARY",
                             "ACME PAYROLL", "TRANSFER"))
            balance += monthly_income

        if secondary_income > 0.0:
            txns.append(_txn(_day(m_start, 15), +secondary_income,
                             "UPWORK PAYMENT", "UPWORK", "TRANSFER"))
            balance += secondary_income

        if i in bonus:
            txns.append(_txn(_day(m_start, income_day), +float(bonus[i]),
                             "ACME PAYROLL BONUS", "ACME PAYROLL", "TRANSFER"))
            balance += float(bonus[i])

        # --- loan direct debit (2nd), possibly missed / late ---
        if i not in missed:
            loan_day = _day(m_start, 2 + int(late.get(i, 0)))
            txns.append(_txn(loan_day, -loan_instalment, "ACME LOANS LTD DD",
                             "ACME LOANS LTD", "DIRECT_DEBIT"))
            balance -= loan_instalment

        # --- credit-card payment (5th) ---
        txns.append(_txn(_day(m_start, 5), -card_payment, "BARCLAYCARD PAYMENT",
                         "BARCLAYCARD", "DIRECT_DEBIT"))
        balance -= card_payment

        # --- rent standing order (1st) ---
        txns.append(_txn(m_start, -rent, "CITY LETTINGS RENT",
                         "CITY LETTINGS", "STANDING_ORDER"))
        balance -= rent

        # --- returned direct debit + fee (NSF): a strong distress signal ---
        if i in nsf:
            txns.append(_txn(_day(m_start, 6), -12.0,
                             "RETURNED DD FEE", "BANK", "FEE"))
            balance -= 12.0

        # --- everyday debits so the account looks alive ---
        txns.append(_txn(_day(m_start, 12), -grocery_monthly, "TESCO STORES",
                         "TESCO", "PURCHASE"))
        txns.append(_txn(_day(m_start, 20), -fuel_monthly, "SHELL FUEL",
                         "SHELL", "PURCHASE"))
        balance -= (grocery_monthly + fuel_monthly)

        if gambling_monthly > 0.0:
            txns.append(_txn(_day(m_start, 18), -gambling_monthly,
                             "BET365 STAKE", "BET365", "PURCHASE"))
            balance -= gambling_monthly

        if savings_monthly > 0.0:
            txns.append(_txn(_day(m_start, 26), -savings_monthly,
                             "TRANSFER TO SAVINGS", "SELF", "TRANSFER"))
            balance -= savings_monthly

        # weekly balance snapshots (days 7 / 14 / 21 / 28)
        for dd in (7, 14, 21):
            snap(_day(m_start, dd))

        # --- optional overdraft episode: dip negative mid-month, repay ---
        if i in od:
            amt = float(od[i])
            txns.append(_txn(_day(m_start, 22), -amt, "CASH WITHDRAWAL",
                             "ATM", "CASH"))
            balance -= amt
            snap(_day(m_start, 23))
            txns.append(_txn(_day(m_start, 27), +amt, "TRANSFER IN",
                             "SELF", "TRANSFER"))
            balance += amt

        snap(_day(m_start, 28))

    accounts = [{
        "account_id": "mock-current-001",
        "account_type": "TRANSACTION",
        "transactions": txns,
        "balances": balances,
        "direct_debits": [
            {"merchant_name": "ACME LOANS LTD", "name": "ACME LOANS LTD",
             "amount": round(loan_instalment, 2)},
            {"merchant_name": "BARCLAYCARD", "name": "BARCLAYCARD",
             "amount": round(card_payment, 2)},
        ],
        "standing_orders": [
            {"merchant_name": "CITY LETTINGS", "name": "CITY LETTINGS RENT",
             "amount": round(rent, 2)},
        ],
    }]

    # --- optional savings account, mirroring the monthly sweep ---
    if savings_balance > 0.0 or savings_monthly > 0.0:
        s_txns: List[dict] = []
        s_bal = float(savings_balance)
        s_snaps: List[dict] = []
        for i in range(months):
            m_start = _month_start(as_of, months - 1 - i)
            if savings_monthly > 0.0:
                s_txns.append(_txn(_day(m_start, 26), +savings_monthly,
                                   "TRANSFER FROM CURRENT", "SELF", "TRANSFER"))
                s_bal += savings_monthly
            s_snaps.append({"current": round(s_bal, 2),
                            "timestamp": _ts(_day(m_start, 28))})
        accounts.append({
            "account_id": "mock-savings-001",
            "account_type": "SAVINGS",
            "transactions": s_txns,
            "balances": s_snaps,
            "direct_debits": [],
            "standing_orders": [],
        })

    return {
        "case_id": case_id,
        "as_of": as_of.isoformat(),
        "accounts": accounts,
        "declared": dict(declared or {}),
    }


# Ready-made risk profiles for the "connect a bank" demo button. Each sets the
# open-banking behaviour; the declared block always comes from the form.
PROFILES = {
    "clean": dict(loan_instalment=320.0, card_payment=120.0, rent=850.0,
                  missed_cycles=[], late_cycles={}, opening_balance=2600.0,
                  savings_balance=4000.0, savings_monthly=150.0,
                  label="Clean payer (no arrears, healthy balance, saving monthly)"),
    "realistic": dict(loan_instalment=295.0, card_payment=110.0, rent=925.0,
                      missed_cycles=[], late_cycles={7: 3, 16: 5},
                      opening_balance=1400.0,
                      savings_balance=1800.0, savings_monthly=75.0,
                      income_freq="4weekly", income_day=26,
                      secondary_income=180.0,
                      bonus_months={11: 1200.0, 23: 1400.0},
                      overdraft_cycles={4: 900.0, 13: 750.0},
                      gambling_monthly=45.0,
                      label="Realistic (2yr history, 4-weekly pay, bonus, "
                            "occasional overdraft, minor lates)"),
    "thin": dict(loan_instalment=180.0, card_payment=60.0, rent=780.0,
                 missed_cycles=[], late_cycles={4: 6}, opening_balance=650.0,
                 label="Thin file (low balance, one late payment)"),
    "arrears": dict(loan_instalment=360.0, card_payment=140.0, rent=900.0,
                    missed_cycles=[3, 7, 10, 15, 19], late_cycles={5: 12, 8: 20, 17: 26},
                    opening_balance=300.0,
                    gambling_monthly=260.0,
                    overdraft_cycles={6: 700.0, 9: 850.0, 14: 900.0, 20: 950.0},
                    nsf_cycles=[7, 10, 19],
                    label="Arrears (missed payments, overdraft, returned DDs)"),
}

_PASSTHROUGH = (
    "loan_instalment", "card_payment", "rent", "missed_cycles", "late_cycles",
    "opening_balance", "months", "income_day", "income_freq",
    "secondary_income", "bonus_months", "savings_balance", "savings_monthly",
    "gambling_monthly", "overdraft_cycles", "nsf_cycles", "grocery_monthly",
    "fuel_monthly",
)


def build_from_profile(case_id: str, as_of: date, monthly_income: float,
                       profile: str, declared: Dict[str, object],
                       months: int = DEFAULT_MONTHS) -> dict:
    p = dict(PROFILES.get(profile, PROFILES["clean"]))
    p.pop("label", None)
    kwargs = {k: v for k, v in p.items() if k in _PASSTHROUGH}
    kwargs.setdefault("months", months)
    return make_payload(case_id=case_id, as_of=as_of,
                        monthly_income=monthly_income, declared=declared,
                        **kwargs)


def build_custom(case_id: str, as_of: date, monthly_income: float,
                 spec: Dict[str, object], declared: Dict[str, object]) -> dict:
    """Build a payload from a caller-supplied spec, starting from the
    'realistic' profile and overriding whatever the caller names. Unknown keys
    are ignored so the UI can send extra fields safely.
    """
    base = dict(PROFILES["realistic"])
    base.pop("label", None)
    base.update({k: v for k, v in dict(spec or {}).items() if k in _PASSTHROUGH})
    kwargs = {k: v for k, v in base.items() if k in _PASSTHROUGH}
    kwargs.setdefault("months", DEFAULT_MONTHS)
    # int-keyed dicts arrive from JSON as strings
    for key in ("late_cycles", "bonus_months", "overdraft_cycles"):
        v = kwargs.get(key)
        if isinstance(v, dict):
            kwargs[key] = {int(k): float(x) for k, x in v.items()}
    return make_payload(case_id=case_id, as_of=as_of,
                        monthly_income=monthly_income, declared=declared,
                        **kwargs)
