"""FeaturePipeline: canonical applicants -> feature matrix (pandas DataFrame).

This is the single execution path for BOTH sources. You give it a list of
CanonicalApplicant (from any adapter) and it returns one row per applicant with
one column per registered feature. Because the feature functions are shared,
running this on Kaggle-derived vs TrueLayer-derived applicants is the apples-to-
apples comparison the parity suite checks.
"""
from __future__ import annotations
from typing import List, Optional

import pandas as pd

from .canonical import CanonicalApplicant
from .config import DEFAULT, EngineConfig
from .feature_functions import FeatureContext  # noqa: F401 (ensures registration)
from .feature_registry import REGISTRY, FeatureSpec
from .logging_utils import get_logger

log = get_logger("pipeline")


class FeaturePipeline:
    def __init__(self, cfg: EngineConfig = DEFAULT, features: Optional[List[FeatureSpec]] = None):
        self.cfg = cfg
        self.features = features or REGISTRY.all()

    def build_row(self, applicant: CanonicalApplicant) -> dict:
        ctx = FeatureContext(applicant, self.cfg)
        row = {"case_id": applicant.case_id}
        for spec in self.features:
            try:
                row[spec.name] = spec.func(ctx)
            except Exception as e:  # never let one feature kill the row
                log.warning("feature %s failed for case %s: %s", spec.name, applicant.case_id, e)
                row[spec.name] = None
        return row

    def build_matrix(self, applicants: List[CanonicalApplicant]) -> pd.DataFrame:
        rows = [self.build_row(a) for a in applicants]
        df = pd.DataFrame(rows).set_index("case_id").sort_index()
        log.info("feature matrix: %d rows x %d features", df.shape[0], df.shape[1])
        return df

    # convenience: surface the monotonic-constraint vector for XGBoost later
    def monotone_constraints(self, columns: List[str]) -> List[int]:
        m = REGISTRY.monotone_map()
        return [m.get(c, 0) for c in columns]
