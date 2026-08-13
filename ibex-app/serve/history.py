"""BUILD 19 -- realised history measurement from a TrueLayer payload.

Why this file exists
--------------------
The v4 dashboard reported 84.3 months of history for a connection that had
asked TrueLayer for 24 months and received 731 days (24.0 months) of
transactions across five accounts.

The old measurement walked the entire payload, collected anything that parsed
as a date, and took max - min. That greedily picked up:

  * standing_orders[].first_payment_date / final_payment_date
  * direct_debits[].previous_payments[].timestamp
  * account mandate and provider onboarding dates

Sandbox standing orders carry first_payment_date values years before the
transaction window opens, so max - min stretched to roughly 2,565 days.

This module measures history from TRANSACTIONS ONLY. A transaction is a dict
that carries BOTH a usable date AND a numeric amount. Direct debit and
standing order subtrees are skipped outright rather than filtered afterwards.

Pure standard library. No numpy, no pandas, importable anywhere.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

DAYS_PER_MONTH = 30.44

# Keys that may carry a transaction's own date.
_TXN_DATE_KEYS: Tuple[str, ...] = (
    "timestamp",
    "booking_date",
    "booking_datetime",
    "value_date",
    "transaction_date",
    "posted_date",
    "date",
)

# Keys that indicate a monetary amount, i.e. that this really is a transaction.
_AMOUNT_KEYS: Tuple[str, ...] = (
    "amount",
    "transaction_amount",
    "amount_in_minor",
    "value",
)

# Subtrees never descended into. These are the source of the inflation.
_EXCLUDE_KEYS = frozenset(
    (
        "direct_debits",
        "direct_debit",
        "standing_orders",
        "standing_order",
        "scheduled_payments",
        "previous_payments",
        "mandate",
        "mandates",
        "provider",
        "consent",
        "meta",
        "metadata",
    )
)

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_MAX_DEPTH = 8


def _parse_date(value: Any) -> Optional[date]:
    """Pull a calendar date out of an ISO-8601 string or datetime."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    m = _DATE_RE.search(value)
    if not m:
        return None
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1970 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
            return None
        return date(y, mo, d)
    except (ValueError, TypeError):
        return None


def _has_amount(node: Dict[str, Any]) -> bool:
    """True when the dict carries something that reads as a money amount."""
    for key in _AMOUNT_KEYS:
        if key not in node:
            continue
        v = node[key]
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return True
        if isinstance(v, str):
            try:
                float(v)
                return True
            except ValueError:
                continue
        if isinstance(v, dict):
            # e.g. {"amount": {"value": 12.34, "currency": "GBP"}}
            for inner in ("value", "amount", "minor_units"):
                iv = v.get(inner)
                if isinstance(iv, (int, float)) and not isinstance(iv, bool):
                    return True
    return False


def _one_date(node: Dict[str, Any]) -> Optional[date]:
    """The transaction's own date, taking the first recognised key."""
    for key in _TXN_DATE_KEYS:
        if key in node:
            parsed = _parse_date(node[key])
            if parsed is not None:
                return parsed
    return None


def _walk(node: Any, depth: int = 0) -> Iterable[Dict[str, Any]]:
    """Yield every dict in the payload, skipping excluded subtrees."""
    if depth > _MAX_DEPTH:
        return
    if isinstance(node, dict):
        yield node
        for k, v in node.items():
            if isinstance(k, str) and k.lower() in _EXCLUDE_KEYS:
                continue
            if isinstance(v, (dict, list)):
                for child in _walk(v, depth + 1):
                    yield child
    elif isinstance(node, list):
        for v in node:
            if isinstance(v, (dict, list)):
                for child in _walk(v, depth + 1):
                    yield child


def _looks_like_transaction(node: Dict[str, Any]) -> bool:
    return _one_date(node) is not None and _has_amount(node)


def _transaction_records(payload: Any) -> List[date]:
    out: List[date] = []
    for node in _walk(payload):
        if _looks_like_transaction(node):
            d = _one_date(node)
            if d is not None:
                out.append(d)
    return out


def collect_transaction_dates(payload: Any) -> List[date]:
    """Sorted transaction dates found in the payload."""
    return sorted(_transaction_records(payload))


def realised_history(
    payload: Any, months_requested: Optional[int] = None
) -> Dict[str, Any]:
    """Measure how much transaction history a connection actually returned.

    Returns a dict safe to merge straight into a score payload.
    """
    dates = collect_transaction_dates(payload)
    result: Dict[str, Any] = {
        "history_start": None,
        "history_end": None,
        "history_days": 0,
        "months_of_history": 0.0,
        "history_transactions": len(dates),
        "months_requested": months_requested,
        "history_measurement": "transactions only (date + amount)",
        "history_warning": None,
    }
    if not dates:
        result["history_warning"] = "no transactions found in payload"
        return result

    start, end = dates[0], dates[-1]
    days = (end - start).days
    months = round(days / DAYS_PER_MONTH, 1)

    result["history_start"] = start.isoformat()
    result["history_end"] = end.isoformat()
    result["history_days"] = days
    result["months_of_history"] = months

    if months_requested is not None and months > float(months_requested) + 1.0:
        result["history_warning"] = (
            "realised %.1f months exceeds the %d requested; "
            "non-transaction dates may still be leaking in"
            % (months, int(months_requested))
        )
    return result


def count_credit_accounts(payload: Any) -> int:
    """Count cards and credit-type accounts. Current/savings do not count."""
    seen = set()
    credit_words = ("credit", "card", "loan", "mortgage", "overdraft")
    for node in _walk(payload):
        aid = node.get("account_id") or node.get("card_id") or node.get("id")
        if not isinstance(aid, str):
            continue
        blob = " ".join(
            str(node.get(k, "")).lower()
            for k in ("account_type", "card_type", "type", "product_type", "card_network")
        )
        if any(w in blob for w in credit_words):
            seen.add(aid)
    return len(seen)


__all__ = [
    "DAYS_PER_MONTH",
    "collect_transaction_dates",
    "realised_history",
    "count_credit_accounts",
]
