"""
Plain-English advice for the 16 features the live model actually uses.

Why this exists
---------------
_issues_from_factors() in ibex_v3.py carried an advice table written for an
older feature set. Only 5 of its 15 keys survive in artifacts_v5:

    longest_clean_streak_24m, total_overdue_amount, debt_to_income,
    monthly_income, declared_income_gap

The other 11 live features had no entry and fell through to the generic
branch, which renders the raw column name -- "Pct dpd payments 24m",
"Dpd late autocorr lag1 24m". That is the uninformative output users see.
Ten stale keys (max_dpd_24m, min_balance_3m, cv_payment_amount and friends)
refer to columns the model no longer has and can never fire.

This table is keyed on scorecard.json["features"] and is checked against it
by coverage_report(). Presentation only -- ranking still comes from the
booster's SHAP contributions.

Each entry:
    title  -- headline when the feature pushes the score DOWN
    detail -- what to actually do about it
    good   -- headline when it pushes the score UP
    fixed  -- True if the user cannot act on it this month; the UI says so
              rather than implying an action exists
"""

from typing import Any, Dict, List

ADVICE: Dict[str, Dict[str, Any]] = {

    # ---- repayment behaviour: 7 of the 12 real features are DPD-derived,
    # so this block drives most scores.
    "pct_dpd_payments_24m": {
        "title": "Payments often late",
        "detail": "This is the share of your last 24 months of payments that "
                  "arrived late, and it is the single strongest driver in the "
                  "model. A Direct Debit dated two days after payday fixes "
                  "most of it.",
        "good": "Payments almost always on time",
    },
    "agg_dpd_count": {
        "title": "Several late payments on record",
        "detail": "How many separate late events sit in your file. Frequency "
                  "counts for more than size, and events drop out as they "
                  "pass 24 months old.",
        "good": "Very few late events on record",
    },
    "longest_clean_streak_24m": {
        "title": "Longest clean run is short",
        "detail": "Your best unbroken run of on-time payments. The model "
                  "rewards a long streak more than a merely low average, so "
                  "consecutive clean months are worth more than scattered "
                  "ones.",
        "good": "Long unbroken run of on-time payments",
    },
    "current_clean_streak_24m": {
        "title": "Recent clean run is short",
        "detail": "Months since your most recent late payment. This one "
                  "rebuilds on its own -- every additional clean month adds "
                  "to it.",
        "good": "Currently on a long clean run",
    },
    "dpd_late_autocorr_lag1_24m": {
        "title": "Late payments come in clusters",
        "detail": "When you are late one month you tend to be late the next. "
                  "That pattern points to cash-flow timing rather than "
                  "forgetfulness -- moving due dates to just after payday "
                  "usually breaks it.",
        "good": "Late payments are isolated, not clustered",
    },
    "dpd_late_autocorr_lag2_24m": {
        "title": "Late payments persist over months",
        "detail": "Lateness carries across two-month gaps, which reads as a "
                  "recurring squeeze rather than a one-off. Spreading due "
                  "dates across the month usually helps.",
        "good": "No multi-month pattern of lateness",
    },
    "total_overdue_amount": {
        "title": "Money currently overdue",
        "detail": "Clearing outstanding arrears is the fastest change "
                  "available to you -- it is the one input that can move "
                  "immediately rather than ageing out over months.",
        "good": "Nothing currently overdue",
    },
}

ADVICE.update({

    # ---- affordability ----
    "debt_to_income": {
        "title": "Debt is high next to income",
        "detail": "Total borrowing measured against the income we can see. "
                  "Paying down balances moves this faster than earning more "
                  "does.",
        "good": "Debt is comfortable next to income",
    },
    "monthly_income": {
        "title": "Detected income is low",
        "detail": "The recurring salary-like inflow found in your connected "
                  "accounts. If income also lands somewhere you have not "
                  "connected, connect it -- the model only counts what it "
                  "can see.",
        "good": "Strong, regular detected income",
    },

    # ---- declared at signup ----
    "declared_income_gap": {
        "title": "Stated and observed income disagree",
        "detail": "What you declared is higher than what arrives in the "
                  "account. Usually this means income lands in an account we "
                  "cannot see; connecting it closes the gap.",
        "good": "Stated income matches what we observed",
    },
    "declared_income_type_code": {
        "title": "Income type carries more risk",
        "detail": "Self-employed and variable income are harder to verify "
                  "than salaried pay, so the model is more cautious. Nothing "
                  "to fix -- it is a property of how you are paid.",
        "good": "Stable income type",
        "fixed": True,
    },
    "declared_employment_code": {
        "title": "Short time with current employer",
        "detail": "Tenure builds on its own. There is no action here this "
                  "month.",
        "good": "Long tenure with your employer",
        "fixed": True,
    },

    # ---- data-quality flags: not risk, but they do move the score ----
    "income_detected": {
        "title": "No regular salary identified",
        "detail": "We could not find a recurring wage in the connected "
                  "accounts, so you are scored as if income were unknown. "
                  "Connecting the account your wages are paid into is the "
                  "highest-value thing you can do.",
        "good": "Regular salary identified",
    },
    "thin_file": {
        "title": "Limited history to judge",
        "detail": "There is not much transaction history yet, so the model "
                  "stays near the population average. This improves with "
                  "time and continued account activity.",
        "good": "Plenty of history to judge",
        "fixed": True,
    },
    "declared_provided": {
        "title": "Signup details incomplete",
        "detail": "You left declared fields blank, so the model fell back on "
                  "population averages. Completing them lets it use your "
                  "actual circumstances.",
        "good": "Signup details complete",
    },
    "n_features_missing": {
        "title": "Some inputs could not be computed",
        "detail": "Each missing input is replaced by a population median, "
                  "which pulls your score toward average in both directions. "
                  "Connecting more accounts reduces the count.",
        "good": "All inputs were available",
    },
})


GENERIC_DOWN = ("This input is pushing your score down relative to the "
                "average applicant.")
GENERIC_UP = "This input is working in your favour."


def _key_of(f: Any) -> str:
    """Reason codes arrive as bare strings or as 'name (value)'."""
    return str(f).split("(")[0].strip()


def build_issues(payload: Dict[str, Any], limit: int = 4) -> List[Dict[str, str]]:
    """Negative drivers as {feature, title, detail, actionable}."""
    out: List[Dict[str, str]] = []
    for f in (payload.get("negative_factors") or [])[:limit]:
        key = _key_of(f)
        a = ADVICE.get(key)
        if a:
            out.append({
                "feature": key,
                "title": a["title"],
                "detail": a["detail"],
                "actionable": not a.get("fixed", False),
            })
        else:
            out.append({
                "feature": key,
                "title": key.replace("_", " ").capitalize(),
                "detail": GENERIC_DOWN,
                "actionable": False,
            })
    return out


def build_positives(payload: Dict[str, Any], limit: int = 4) -> List[Dict[str, str]]:
    """Positive drivers, phrased as strengths rather than column names."""
    out: List[Dict[str, str]] = []
    for f in (payload.get("positive_factors") or [])[:limit]:
        key = _key_of(f)
        a = ADVICE.get(key)
        out.append({
            "feature": key,
            "title": a["good"] if a else key.replace("_", " ").capitalize(),
            "detail": "" if a else GENERIC_UP,
        })
    return out


def coverage_report(features: List[str]) -> Dict[str, List[str]]:
    """
    Guard against this table drifting out of sync with the model again.
    Pass scorecard.json['features']; 'missing' must stay empty.
    """
    have = set(ADVICE)
    return {
        "missing": [f for f in features if f not in have],
        "stale": sorted(have - set(features)),
    }
