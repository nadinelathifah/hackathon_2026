"""Join the reconstructed feature matrix back to the labels, by case_id.

run_kaggle.py writes a feature matrix indexed by case_id but WITHOUT the target
or WEEK_NUM (those live in the raw competition `base` table). CreditDataset
re-reads train_base.parquet from the data directory and inner-joins on case_id,
so the modelling code always sees features + target + week aligned row-for-row.
"""
from __future__ import annotations
import glob
import os
from dataclasses import dataclass
from typing import List

import numpy as np
import pandas as pd

from ..adapters.kaggle_adapter import _logical_stem


@dataclass
class CreditDataset:
    features: pd.DataFrame      # index = case_id (str), numeric feature columns
    target: pd.Series           # aligned to features.index
    week: pd.Series             # aligned to features.index
    feature_names: List[str]

    @classmethod
    def from_files(cls, features_path: str, base_dir: str,
                   target_col: str = "target",
                   week_col: str = "WEEK_NUM") -> "CreditDataset":
        feats = _read_any(features_path)
        if "case_id" in feats.columns:
            feats = feats.set_index("case_id")
        feats.index = feats.index.astype(str)

        base = _read_base(base_dir, target_col, week_col)
        base.index = base.index.astype(str)

        joined = feats.join(base, how="inner")
        if target_col not in joined.columns:
            raise KeyError(f"target column '{target_col}' not found in base table")
        joined = joined[~joined[target_col].isna()]

        y = joined[target_col].astype(float)
        wk = (joined[week_col].astype(float) if week_col in joined.columns
              else pd.Series(0.0, index=joined.index))
        drop = [c for c in (target_col, week_col) if c in joined.columns]
        X = joined.drop(columns=drop).select_dtypes(include=[np.number])
        return cls(features=X, target=y, week=wk, feature_names=list(X.columns))


def _read_any(path: str) -> pd.DataFrame:
    if path.endswith(".csv"):
        return pd.read_csv(path)
    try:
        return pd.read_parquet(path)
    except Exception:
        alt = os.path.splitext(path)[0] + ".csv"
        if os.path.exists(alt):
            return pd.read_csv(alt)
        raise


def _read_base(base_dir: str, target_col: str, week_col: str) -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(base_dir, "*.parquet")))
    base_files = [f for f in files
                  if _logical_stem(os.path.splitext(os.path.basename(f))[0]) == "base"]
    if not base_files:
        raise FileNotFoundError(
            f"no base parquet (e.g. train_base.parquet) found in {base_dir}")
    want = ["case_id", week_col, target_col]
    import pyarrow.parquet as pq
    parts = []
    for f in base_files:
        names = pq.ParquetFile(f).schema.names
        cols = [c for c in want if c in names]
        parts.append(pd.read_parquet(f, columns=cols))
    base = pd.concat(parts, ignore_index=True).set_index("case_id")
    return base
