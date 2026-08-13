"""BUILD 15 -- serving core.

Loads the artifacts produced by scripts/calibrate_score.py and turns ONE
open-banking payload into a calibrated PD, a PDO credit score, a risk band and
adverse-action reason codes.

Why this is defensible:
  * It runs the identical f() used in training/eval: TrueLayerAdapter ->
    FeaturePipeline (REGISTRY.all()) -> the same feature columns.
  * It imputes missing features with the SAME training-slice medians
    (scorecard.json -> "medians"), then 0.0 -- byte-for-byte what _prep_lgbm did
    offline. So a served score equals the offline score for the same payload.
  * Ranking (Gini) is set by the booster; the isotonic calibrator only turns the
    raw score into a true probability; the PDO scorecard is a transparent,
    monotone log-odds transform. Nothing here re-learns or leaks.

Artifacts expected in --artifacts dir (default: <project>/artifacts):
  model_lgbm.txt    LightGBM booster (text model)
  calibrator.pkl    IsotonicCalibrator (raw score -> PD)
  scorecard.json    features, medians, monotone, best_iteration, pdo/base_*/bands
"""
from __future__ import annotations
import json
import os
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from obcredit.pipeline import FeaturePipeline                     # noqa: E402
from obcredit.adapters.truelayer_adapter import TrueLayerAdapter  # noqa: E402
from obcredit.modeling.calibration import IsotonicCalibrator      # noqa: E402
from obcredit.modeling.scorecard import (pd_to_score, score_to_band,  # noqa: E402
                                         top_reason_codes)

DEFAULT_ARTIFACTS = os.path.join(_ROOT, "artifacts")

# Friendly, plain-English labels for the model's feature names (for reason
# codes). Anything not listed falls back to a prettified version of the name.
FEATURE_LABELS: Dict[str, str] = {
    "num_dpd_events_24m": "Recent missed / late payments",
    "max_dpd_24m": "Worst recent delinquency (days past due)",
    "total_overdue_amount": "Amount currently overdue",
    "mean_dpd_24m": "Average lateness on repayments",
    "dpd_trend": "Payment behaviour is worsening",
    "debt_to_income": "Debt-to-income ratio",
    "instalment_to_income": "Monthly repayments vs income",
    "exposure_to_income": "Total credit exposure vs income",
    "num_active_obligations": "Number of active credit lines",
    "utilisation": "Credit utilisation",
    "min_balance_3m": "Lowest recent account balance",
    "mean_balance_3m": "Average recent account balance",
    "num_low_balance_days": "Days spent near a zero balance",
    "monthly_income": "Detected monthly income",
    "income_cv": "Income volatility",
    "payment_cv": "Repayment-amount volatility",
    "declared_income_is_employment": "Declared income is employment/salary",
    "declared_is_homeowner": "Declared home ownership",
    "declared_income_type_code": "Declared income type",
    "declared_education_code": "Declared education level",
    "declared_housing_code": "Declared housing situation",
    "declared_employment_code": "Declared employment tenure",
    "declared_income_gap": "Gap between stated and observed income",
}


def label_for(name: str) -> str:
    return FEATURE_LABELS.get(name, name.replace("_", " ").strip().capitalize())


class ScoringService:
    """Stateless-after-load scorer. Construct once, call score_payload() often."""

    def __init__(self, artifacts_dir: str = DEFAULT_ARTIFACTS):
        import lightgbm as lgb  # imported here so importing this module is cheap

        self.artifacts_dir = artifacts_dir
        card_path = os.path.join(artifacts_dir, "scorecard.json")
        model_path = os.path.join(artifacts_dir, "model_lgbm.txt")
        cal_path = os.path.join(artifacts_dir, "calibrator.pkl")
        for p in (card_path, model_path, cal_path):
            if not os.path.exists(p):
                raise FileNotFoundError(
                    f"missing artifact: {p}\nRun scripts/calibrate_score.py first "
                    f"and point --artifacts at its output dir.")

        with open(card_path, "r", encoding="utf-8") as f:
            self.card = json.load(f)
        self.features: List[str] = list(self.card["features"])
        self.medians: Dict[str, float] = dict(self.card.get("medians", {}))
        self.pdo = float(self.card.get("pdo", 40.0))
        self.base_score = float(self.card.get("base_score", 600.0))
        self.base_odds = float(self.card.get("base_odds", 20.0))
        bi = self.card.get("best_iteration")
        self.best_iteration: Optional[int] = int(bi) if bi else None

        self.booster = lgb.Booster(model_file=model_path)
        self.calibrator = IsotonicCalibrator.load(cal_path)
        self.pipe = FeaturePipeline()

    # ------------------------------------------------------------------ core
    def _vectorize(self, applicant) -> Tuple[pd.DataFrame, Dict[str, float]]:
        """Build the 1-row model matrix in the trained column order, coercing to
        numeric and imputing with the training medians (then 0.0)."""
        raw = self.pipe.build_matrix([applicant])
        used_raw: Dict[str, float] = {}
        X = pd.DataFrame(index=raw.index)
        for c in self.features:
            if c in raw.columns:
                col = pd.to_numeric(raw[c], errors="coerce")
            else:
                col = pd.Series([np.nan] * len(raw), index=raw.index)
            v = col.iloc[0]
            if pd.notna(v):
                used_raw[c] = float(v)
            fill = float(self.medians.get(c, 0.0))
            X[c] = col.fillna(fill).fillna(0.0)
        return X, used_raw

    def score_payload(self, payload: dict) -> dict:
        """payload is a TrueLayer fetch_user()-shaped dict (see mock_ob.py)."""
        applicants = TrueLayerAdapter([payload]).to_canonical()
        if not applicants:
            raise ValueError("payload produced no applicant")
        applicant = applicants[0]
        X, used_raw = self._vectorize(applicant)
        Xnp = X.to_numpy(np.float32)

        raw = float(self.booster.predict(Xnp, num_iteration=self.best_iteration)[0])
        pd_hat = float(self.calibrator.predict([raw])[0])
        score = float(pd_to_score(pd_hat, self.pdo, self.base_score, self.base_odds))
        band = score_to_band(score)

        # per-feature contributions on the raw margin (last element is the bias).
        contribs = self.booster.predict(
            Xnp, num_iteration=self.best_iteration, pred_contrib=True)[0]
        feat_contribs = list(np.asarray(contribs, dtype=float)[:len(self.features)])
        pos = top_reason_codes(feat_contribs, self.features, k=4)          # risk up
        order_down = sorted(zip(self.features, feat_contribs), key=lambda kv: kv[1])
        neg = order_down[:4]                                               # risk down

        def _fmt(pairs):
            return [{"feature": n, "label": label_for(n),
                     "contribution": float(c),
                     "value": used_raw.get(n)} for n, c in pairs]

        return {
            "case_id": applicant.case_id,
            "score": round(score, 1),
            "band": band,
            "pd": round(pd_hat, 4),
            "raw_margin": round(raw, 4),
            "monthly_income_detected": (round(float(applicant.monthly_income), 2)
                                        if applicant.monthly_income else None),
            "num_obligations": len(applicant.obligations),
            "declared": dict(getattr(applicant, "declared", {}) or {}),
            "adverse_reasons": _fmt([(n, c) for n, c in pos if c > 0]),
            "positive_reasons": _fmt([(n, c) for n, c in neg if c < 0]),
            "build": self.card.get("build"),
            "num_features": len(self.features),
        }
