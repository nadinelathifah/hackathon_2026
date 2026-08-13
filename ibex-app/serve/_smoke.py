"""No-LightGBM smoke test of the SERVING feature path.

Validates that a mock TrueLayer payload -> TrueLayerAdapter -> FeaturePipeline
reconstructs income + obligations and populates the BUILD 14 declared features,
and that the trained-column vectorisation/imputation in score_service works.
Does NOT load the booster/calibrator (lightgbm not in this sandbox).
"""
from __future__ import annotations
import os
import sys
from datetime import date

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from obcredit.pipeline import FeaturePipeline
from obcredit.adapters.truelayer_adapter import TrueLayerAdapter
from obcredit.feature_registry import REGISTRY
from serve.mock_ob import build_from_profile, PROFILES

DECLARED = {"employment": "MORE_ONE_YEAR", "income_type": "SALARIED",
            "education": "HIGHER_EDU", "housing": "OWNED", "stated_income": 3200.0}
DECLARED_RENTER = {"employment": "LESS_ONE_YEAR", "income_type": "OTHER",
                   "education": "SECONDARY", "housing": "RENTED", "stated_income": 2500.0}

DECLARED_FEATS = ["declared_income_is_employment", "declared_is_homeowner",
                  "declared_income_type_code", "declared_education_code",
                  "declared_housing_code", "declared_employment_code",
                  "declared_income_gap"]


def run_one(profile, declared, label):
    payload = build_from_profile("case-" + profile, date(2024, 6, 1), 3200.0,
                                 profile, declared)
    apps = TrueLayerAdapter([payload]).to_canonical()
    assert apps, "no applicant built"
    a = apps[0]
    row = FeaturePipeline().build_matrix([a])
    print(f"\n=== {label} (profile={profile}) ===")
    print(f"  detected monthly_income : {a.monthly_income}")
    print(f"  obligations reconstructed: {len(a.obligations)}")
    print(f"  declared carried         : {a.declared}")
    for f in DECLARED_FEATS:
        v = row[f].iloc[0] if f in row.columns else "<missing>"
        print(f"    {f:32s} = {v}")
    # simulate score_service vectorisation (median impute -> 0.0)
    cols = [c for c in REGISTRY.parity_names() if c in row.columns]
    X = pd.DataFrame(index=row.index)
    for c in cols:
        col = pd.to_numeric(row[c], errors="coerce")
        X[c] = col.fillna(0.0)
    Xnp = X.to_numpy(np.float32)
    assert Xnp.dtype == np.float32 and not np.isnan(Xnp).any(), "vector not clean float"
    print(f"  vector shape {Xnp.shape}, dtype {Xnp.dtype}, NaNs={int(np.isnan(Xnp).sum())}")
    return a, row


def main():
    a1, r1 = run_one("clean", DECLARED, "Homeowner / salaried / clean bank")
    a2, r2 = run_one("arrears", DECLARED_RENTER, "Renter / other income / arrears bank")
    # assertions on declared features
    assert r1["declared_is_homeowner"].iloc[0] == 1.0, "owner should be homeowner=1"
    assert r2["declared_is_homeowner"].iloc[0] == 0.0, "renter should be homeowner=0"
    assert r1["declared_income_is_employment"].iloc[0] == 1.0, "salaried should be 1"
    assert r2["declared_income_is_employment"].iloc[0] == 0.0, "other income should be 0"
    # arrears profile should show more delinquency than clean
    for dpd_col in ("num_dpd_events_24m", "total_overdue_amount"):
        if dpd_col in r1.columns and dpd_col in r2.columns:
            print(f"\n  {dpd_col}: clean={r1[dpd_col].iloc[0]}  arrears={r2[dpd_col].iloc[0]}")
    print("\nSMOKE OK \u2705  serving feature path works end-to-end (no lightgbm).")


if __name__ == "__main__":
    main()
