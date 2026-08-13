"""Build the feature matrix from a real Home Credit parquet download.

Usage:
    python scripts/run_kaggle.py [parquet_dir] [out.parquet] [max_cases]

  parquet_dir : folder with the competition .parquet files.
                DEFAULT = the folder this script lives in.
  out.parquet : output filename (default 'kaggle_features.parquet').
  max_cases   : optional integer -- SMOKE TEST. Only the first N case_ids are
                read (filter pushed into every parquet file), so it is fast and
                low-memory. Omit it for the full run.

Real Kaggle filenames (train_*, chunked _0/_1/...) are handled automatically,
and only the 7 tables / few columns the engine needs are ever loaded.
"""
from __future__ import annotations
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from obcredit.adapters import KaggleAdapter  # noqa: E402
from obcredit.pipeline import FeaturePipeline  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def main(parquet_dir: str, out_path: str = "kaggle_features.parquet",
         max_cases=None):
    print("=== obcredit BUILD 5 (WORKING: DPD-direct, streaming a_2) ===")
    print(f"Reading parquet files from: {parquet_dir}")
    if max_cases is not None:
        print(f"SMOKE TEST mode: first {max_cases} case_ids only")
    adapter = KaggleAdapter.from_parquet_dir(parquet_dir, max_cases=max_cases)
    # Streaming build: credit_bureau_a_2 (~188M rows) is read one chunk at a time
    # and never concatenated whole, so peak memory stays bounded.
    matrix = adapter.build_matrix_streaming(FeaturePipeline())
    if matrix.empty:
        print("\nNo applicants were built. Check that the folder actually "
              "contains the competition .parquet files (e.g. train_base.parquet, "
              "train_credit_bureau_a_2_*.parquet).")
        return
    matrix.to_parquet(out_path)
    matrix.to_csv(os.path.splitext(out_path)[0] + ".csv")
    print(f"\nwrote {out_path} (and .csv): "
          f"{matrix.shape[0]} rows x {matrix.shape[1]} features")
    print(matrix.head())


if __name__ == "__main__":
    args = sys.argv[1:]
    parquet_dir = args[0] if len(args) >= 1 else SCRIPT_DIR
    out_path = args[1] if len(args) >= 2 else "kaggle_features.parquet"
    max_cases = int(args[2]) if len(args) >= 3 else None
    main(parquet_dir, out_path, max_cases)
