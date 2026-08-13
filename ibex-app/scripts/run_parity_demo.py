"""Offline demo: build matched fixtures, run f() on both, print side-by-side.

No network, no Kaggle download needed. This is the fastest way to SEE that both
sources produce identical features.

    python scripts/run_parity_demo.py
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from obcredit.adapters import KaggleAdapter, TrueLayerAdapter  # noqa: E402
from obcredit.feature_registry import REGISTRY  # noqa: E402
from obcredit.pipeline import FeaturePipeline  # noqa: E402
from fixtures.make_fixtures import build_matched_fixtures  # noqa: E402

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 100)


def main():
    kaggle_frames, tl_payloads = build_matched_fixtures()
    pipe = FeaturePipeline()
    k = pipe.build_matrix(KaggleAdapter(kaggle_frames).to_canonical())
    t = pipe.build_matrix(TrueLayerAdapter(tl_payloads).to_canonical())

    print("\n=== KAGGLE-derived features ===")
    print(k.T)
    print("\n=== TRUELAYER-derived features ===")
    print(t.T)

    print("\n=== ABSOLUTE DIFFERENCE (parity features only) ===")
    cols = REGISTRY.parity_names()
    diff = (k[cols].astype(float) - t[cols].astype(float)).abs()
    print(diff.T)
    worst = diff.max().max()
    print(f"\nMax absolute parity difference across all cases/features: {worst:.3e}")
    print("PARITY OK" if worst < 1e-6 else "PARITY BROKEN")


if __name__ == "__main__":
    main()
