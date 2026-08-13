"""TrueLayer open-banking JSON -> CanonicalApplicant.

The TrueLayer Data API returns, per connected user:
  /data/v1/accounts                     -> account list (id, type, currency)
  /data/v1/accounts/{id}/transactions   -> transactions (timestamp, amount,
                                            description, transaction_category,
                                            merchant_name ...)
  /data/v1/accounts/{id}/balance         -> current/available balance
  /data/v1/accounts/{id}/direct_debits   -> direct debits (recurring)
  /data/v1/accounts/{id}/standing_orders -> standing orders (recurring)
  /data/v1/cards , /cards/{id}/transactions, /cards/{id}/balance

Sign convention: TrueLayer amounts are NEGATIVE for money leaving the account.
We flip to the canonical convention (positive = outflow / repayment).

We reconstruct obligations by running the SHARED RecurringStreamDetector on the
outgoing transactions (plus any declared direct debits / standing orders as
strong hints). Income is the detected recurring salary inflow. Once we have the
canonical objects, the SAME engine + feature library compute the features.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
import re
import statistics

from ..canonical import (CanonicalAccount, CanonicalApplicant,
                         CanonicalObligation, CanonicalPayment)
from ..config import DEFAULT, EngineConfig
from ..logging_utils import get_logger
from ..payment_engine import PaymentScheduleModel, RawTxn, RecurringStreamDetector, _cv
from .base import SourceAdapter

log = get_logger("truelayer_adapter")

_WS = re.compile(r"\s+")

# Salary/payroll payee cues. Matched on WHOLE WORDS against the normalised
# (digit-stripped, lower-cased) counterparty text. Deliberately excludes the
# bare token "pay" -- that substring matched "PAYMENT"/"PAYPAL"/"repayment" and
# was the source of the spurious constant ~£25 detected income.
_SALARY_RE = re.compile(r"\b(salary|salaries|payroll|wages?|hmrc|net\s*pay|paye)\b")


def _norm_counterparty(*parts: Optional[str]) -> str:
    text = " ".join(p for p in parts if p)
    text = text.lower()
    text = re.sub(r"[0-9]+", "", text)            # drop reference numbers
    text = re.sub(r"[^a-z ]", " ", text)
    return _WS.sub(" ", text).strip()


def _to_date(ts: str) -> date:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).date()


class TrueLayerAdapter(SourceAdapter):
    """Map one connected user's TrueLayer payload into a CanonicalApplicant.

    `payload` shape (exactly what TrueLayerDataClient.fetch_user() returns):
      {
        "case_id": "...",
        "as_of": "YYYY-MM-DD",
        "accounts": [
          {"account_id": "..", "account_type": "TRANSACTION|SAVINGS",
           "transactions": [ {transaction} ... ],
           "balances": [ {"current": .., "timestamp": ".."} ],
           "direct_debits": [..], "standing_orders": [..]}
        ],
        "declared": {...}
      }
    """

    def __init__(self, payloads: List[dict], cfg: EngineConfig = DEFAULT):
        self.payloads = payloads
        self.cfg = cfg
        self.detector = RecurringStreamDetector(cfg)

    # ---------------------------------------------------------------- income
    @staticmethod
    def _to_monthly(amount: float, period_days: float) -> float:
        """Per-cycle amount -> monthly figure, from the observed cadence.

        A ~monthly cadence (26-35 days) returns the amount UNCHANGED -- this
        keeps Kaggle/TrueLayer income parity byte-for-byte on monthly salaries
        (the parity fixtures + real payroll are monthly). Weekly / fortnightly
        streams are scaled up to their monthly equivalent."""
        if 26.0 <= period_days <= 35.0:
            return float(amount)
        return float(amount * (30.44 / period_days))

    def _detect_income(self, all_txns: List[dict]) -> Optional[float]:
        """Monthly income via RECURRING-INFLOW detection.

        The old detector took the MEDIAN of every credit whose description
        contained a salary keyword. With the loose token "pay" it matched
        "PAYMENT"/"PAYPAL"/"repayment", and on a sandbox account with no real
        payroll line it collapsed onto tiny generic credits -> a constant ~£25.

        This version mirrors how open-banking affordability engines verify
        income, and is robust to one-off / tiny credits:
          1. keep credits only (amount > 0);
          2. group by normalised payee;
          3. keep groups that RECUR at a stable salary-range cadence with low
             amount dispersion (a single refund can never qualify);
          4. convert each stream to a MONTHLY figure via its cadence;
          5. drop streams below a monthly floor (kills tiny-credit noise);
          6. prefer a salary/payroll-named stream; else fall back to the largest
             qualifying recurring inflow.
        Returns None when nothing recurring and material is found -- honest, so
        the serving layer median-imputes rather than inventing a number."""
        cfg = self.cfg
        groups: Dict[str, List[Tuple[date, float]]] = {}
        for t in all_txns:
            amt = float(t.get("amount", 0.0))
            if amt <= 0:
                continue  # credits only
            ts = t.get("timestamp")
            if not ts:
                continue
            cp = _norm_counterparty(t.get("merchant_name"), t.get("description"),
                                    t.get("transaction_category")) or "unknown"
            groups.setdefault(cp, []).append((_to_date(ts), amt))

        candidates: List[Tuple[float, bool]] = []  # (monthly_income, salary_named)
        for cp, items in groups.items():
            if len(items) < cfg.income_min_events:
                continue
            items.sort(key=lambda p: p[0])
            dates = [d for d, _ in items]
            amts = [a for _, a in items]
            intervals = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
            if len(intervals) < cfg.income_min_events - 1:
                continue
            period = float(statistics.median(intervals))
            if not (cfg.income_period_min_days <= period <= cfg.income_period_max_days):
                continue
            if _cv(amts) > cfg.income_amount_cv_max:
                continue
            monthly = self._to_monthly(float(statistics.median(amts)), period)
            if monthly < cfg.income_min_monthly:
                continue
            candidates.append((monthly, bool(_SALARY_RE.search(cp))))

        if not candidates:
            return None
        salary_named = [m for m, sal in candidates if sal]
        pool = salary_named if salary_named else [m for m, _ in candidates]
        return float(max(pool))

    # ------------------------------------------------------------ obligations
    def _raw_txns(self, account: dict) -> List[RawTxn]:
        out = []
        for t in account.get("transactions", []):
            amt = float(t.get("amount", 0.0))
            # canonical: positive = outflow. TrueLayer debits are negative.
            canonical_amt = -amt
            cp = _norm_counterparty(t.get("merchant_name"), t.get("description"),
                                    t.get("transaction_category"))
            out.append(RawTxn(date=_to_date(t["timestamp"]), amount=canonical_amt,
                              counterparty=cp or "unknown",
                              kind_hint=t.get("transaction_category", "")))
        return out

    def _accounts(self, payload: dict) -> List[CanonicalAccount]:
        accts = []
        for a in payload.get("accounts", []):
            t = a.get("account_type", "TRANSACTION")
            ctype = "savings" if str(t).upper().startswith("SAV") else "current"
            bals = []
            for b in a.get("balances", []):
                ts = b.get("timestamp")
                d = _to_date(ts) if ts else date.fromisoformat(payload["as_of"])
                bals.append((d, float(b.get("current", 0.0))))
            accts.append(CanonicalAccount(account_id=a["account_id"], type=ctype, balances=bals))
        return accts

    def _mandates(self, payload: dict) -> List[Tuple[str, float]]:
        """Recurring mandates (direct debits + standing orders) as
        (normalised payee, scheduled amount). Real open banking exposes these, so
        a credit line is known even when its collections fail to clear. Payee is
        normalised identically to a transaction so mandates and cleared payments
        key to the SAME obligation."""
        out: List[Tuple[str, float]] = []
        for a in payload.get("accounts", []):
            for m in list(a.get("direct_debits", [])) + list(a.get("standing_orders", [])):
                amt = float(m.get("amount", 0.0) or 0.0)
                payee = _norm_counterparty(m.get("merchant_name"), m.get("name"),
                                           m.get("transaction_category"))
                if payee and amt > 0:
                    out.append((payee, amt))
        return out

    def _one(self, payload: dict) -> CanonicalApplicant:
        as_of = date.fromisoformat(payload["as_of"])
        all_txns_raw: List[RawTxn] = []
        all_txns_json: List[dict] = []
        for a in payload.get("accounts", []):
            all_txns_raw.extend(self._raw_txns(a))
            all_txns_json.extend(a.get("transactions", []))
        obligations = self.detector.detect(all_txns_raw)

        # ---- direct-debit / standing-order mandates -----------------------
        # Real open banking lists the recurring mandate for a credit line, so a
        # line is KNOWN (payee + scheduled amount) even when too few of its
        # collections clear to form a >=3-payment detectable stream. We (a) take
        # the authoritative per-line instalment straight from the mandates and
        # (b) SEED an obligation for any mandated line the detector missed,
        # attaching whatever debits did clear so its real lateness still counts.
        mandates = self._mandates(payload)
        detected_cps = {o.obligation_id.split("::", 1)[-1] for o in obligations}
        if mandates:
            debits_by_cp: Dict[str, List[RawTxn]] = {}
            for t in all_txns_raw:
                if t.amount > 0:
                    debits_by_cp.setdefault(t.counterparty, []).append(t)
            for cp, _amt in mandates:
                if cp in detected_cps:
                    continue
                pays = [CanonicalPayment(f"mandate::{cp}", t.date, t.amount)
                        for t in sorted(debits_by_cp.get(cp, []), key=lambda x: x.date)]
                obligations.append(CanonicalObligation(
                    obligation_id=f"mandate::{cp}",
                    kind=self.detector._classify(cp, ""),
                    opened=min((p.date for p in pays), default=as_of),
                    payments=pays,
                ))

        # per-line instalments: prefer the mandate schedule (covers EVERY line);
        # else fall back to the median paid per detected stream. Captured BEFORE
        # missed-cycle imputation so synthetic zero-amount events cannot bias it.
        if mandates:
            instalments = [float(amt) for _cp, amt in mandates if amt]
        else:
            instalments = [float(v) for v in (o.scheduled_instalment() for o in obligations) if v]

        # a missed direct debit is an ABSENT transaction: detect the cadence and
        # fill each empty cycle with an overdue event, so the open-banking overdue
        # series matches the amount the bureau reports as overdue.
        for o in obligations:
            if len(o.payment_dates()) >= 2:      # need >=2 timestamps to infer a schedule
                self._assign_dpd(o)              # DPD from schedule timing on real payments
                self._impute_missed_cycles(o)    # missed cycles -> capped DPD + overdue
        income = self._detect_income(all_txns_json)
        return CanonicalApplicant(
            case_id=str(payload["case_id"]), as_of=as_of,
            obligations=obligations, accounts=self._accounts(payload),
            monthly_income=income, instalments=instalments,
            declared=payload.get("declared", {}),
        )

    def _assign_dpd(self, ob: CanonicalObligation) -> None:
        """Set each OBSERVED payment's days-past-due from the shared schedule model.
        This is the open-banking counterpart to the bureau's reported DPD column:
        we learn the customer's cadence and measure how late each payment landed,
        flooring at 0 and capping identically to the Kaggle side (parity)."""
        fit = PaymentScheduleModel(self.cfg).fit(ob.payment_dates())
        cap = self.cfg.dpd_clip_days
        by_date = {p.actual: max(0, p.dpd) for p in fit.points}
        for p in ob.payments:
            if p.dpd is None:
                p.dpd = float(min(by_date.get(p.date, 0), cap))

    def _impute_missed_cycles(self, ob: CanonicalObligation) -> None:
        """Fill each expected-but-empty payment cycle with a synthetic overdue
        event (overdue = the obligation instalment). This is how a missed/failed
        direct debit becomes an overdue amount that mirrors the bureau column."""
        dates = ob.payment_dates()
        if len(dates) < 3:
            return
        instalment = ob.scheduled_instalment() or 0.0
        if instalment <= 0:
            return
        anchor = dates[0]
        offsets = [(d - anchor).days for d in dates]
        diffs = [b - a for a, b in zip(offsets, offsets[1:])]
        period = statistics.median(diffs)
        if period <= 0:
            return
        present = {0}
        cyc = 0
        for gap in diffs:
            step = max(1, int(round(gap / period)))
            cyc += step
            present.add(cyc)
        for c in range(cyc + 1):
            if c not in present:
                slot = anchor + timedelta(days=int(round(c * period)))
                ob.payments.append(CanonicalPayment(
                    ob.obligation_id, slot, 0.0, overdue=instalment,
                    dpd=float(self.cfg.missed_dpd_cap_days)))

    def to_canonical(self) -> List[CanonicalApplicant]:
        out = [self._one(p) for p in self.payloads]
        log.info("truelayer -> %d canonical applicants", len(out))
        return out
