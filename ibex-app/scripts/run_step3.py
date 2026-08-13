#!/usr/bin/env python3
"""STEP 3 -- prove the reconstruction on REAL Kaggle data by round-tripping the
REAL per-payment history through the open-banking format (no bucketing).

Pipeline (one command):

  1. Stream the real Home Credit tables into CanonicalApplicants using the
     proven, memory-safe KaggleAdapter (the 188M-row bureau table is never held
     in RAM). Each applicant carries its REAL per-payment reported DPD + overdue.
  2. Convert each into open-banking ground truth (step3lib.kaggle_stream.
     canonical_to_ground_truth) -- every scheduled payment keeps its own DPD, so
     there is NO summary-level bucketing.
  3. RENDER each ground truth two ways (step3lib.renderers) and reconstruct via
     the SHARED FeaturePipeline:
        * Kaggle bureau shape   -> KaggleAdapter   (DPD read directly)
        * TrueLayer OB shape     -> TrueLayerAdapter (DPD reconstructed from timing)
     The ONLY thing that can differ between the two is open banking's
     DPD-from-timing limit -- exactly what we are measuring.
  4. STEP 2 (fidelity): compare the open-banking-reconstructed features against
     the Kaggle-direct features, per parity feature.
  5. STEP 1 (model): train the logistic regression on the real target with an
     out-of-time split by week, on BOTH matrices, and print each Gini + verdict.
     The Kaggle-direct Gini is the real feature power; the open-banking Gini is
     the fair reconstruction estimate; their GAP is the cost of open banking.

Usage:
    python scripts/run_step3.py <kaggle_dir> [--max-cases N] [--batch B]
                                            [--leave-one-out]

  <kaggle_dir>   folder with the competition parquet files.
  --max-cases N  sample the first N applicants (default 25000; use 0 for ALL --
                 note the real per-payment path retains sampled cases in memory,
                 so keep this bounded unless you have plenty of RAM).
  --batch B      cases rendered/reconstructed per batch (memory bound).
  --leave-one-out  print each feature's drop-column Gini on the OB matrix.

Test-set Gini verdict: <0.02 NO SIGNAL | <0.10 WEAK | <0.40 REAL | else STRONG.
"""
from __future__ import annotations
import argparse
import glob
import os
import sys

import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from obcredit.adapters import KaggleAdapter, TrueLayerAdapter  # noqa: E402
from obcredit.adapters.kaggle_adapter import _logical_stem  # noqa: E402
from obcredit.feature_registry import REGISTRY  # noqa: E402
from obcredit.pipeline import FeaturePipeline  # noqa: E402

from step3lib.kaggle_stream import canonical_pop_to_ground_truth  # noqa: E402
from step3lib.renderers import to_kaggle_frames, to_truelayer_payloads  # noqa: E402
from step3lib.model import fit_eval, leave_one_out, verdict  # noqa: E402

CASE = "case_id"
WEEK = "WEEK_NUM"
TARGET = "target"


# --------------------------------------------------------------------------- #
# labels (target + competition week) straight from base
# --------------------------------------------------------------------------- #
def read_labels(path: str, max_cases=None):
    import pyarrow.parquet as pq
    by_stem = {}
    for fp in sorted(glob.glob(os.path.join(path, "*.parquet"))):
        stem = _logical_stem(os.path.splitext(os.path.basename(fp))[0])
        if stem == "base":
            by_stem.setdefault(stem, []).append(fp)
    parts = []
    for fp in by_stem.get("base", []):
        names = pq.ParquetFile(fp).schema.names
        cols = [c for c in (CASE, WEEK, TARGET) if c in names]
        parts.append(pd.read_parquet(fp, columns=cols))
    if not parts:
        return {}
    base = pd.concat(parts, ignore_index=True)
    if max_cases:
        base = base.head(max_cases)
    labels = {}
    for _, r in base.iterrows():
        labels[str(r[CASE])] = (int(r.get(TARGET, 0) or 0), int(r.get(WEEK, 0) or 0))
    return labels


# --------------------------------------------------------------------------- #
# render both shapes + reconstruct via the shared engine (batched)
# --------------------------------------------------------------------------- #
def reconstruct(pop, batch: int):
    """Return (ob_matrix, kaggle_matrix): parity features via each source."""
    pipe = FeaturePipeline()
    ob_frames, kg_frames = [], []
    for i in range(0, len(pop), batch):
        chunk = pop[i:i + batch]
        ob_frames.append(pipe.build_matrix(TrueLayerAdapter(to_truelayer_payloads(chunk)).to_canonical()))
        kg_frames.append(pipe.build_matrix(KaggleAdapter(to_kaggle_frames(chunk)).to_canonical()))
    ob = pd.concat(ob_frames) if ob_frames else pd.DataFrame()
    kg = pd.concat(kg_frames) if kg_frames else pd.DataFrame()
    return ob, kg


def fidelity_report(ob: pd.DataFrame, kg: pd.DataFrame) -> pd.DataFrame:
    cols = [c for c in REGISTRY.parity_names() if c in ob.columns and c in kg.columns]
    idx = ob.index.intersection(kg.index)
    rows = []
    for c in cols:
        a = pd.to_numeric(ob.loc[idx, c], errors="coerce")
        b = pd.to_numeric(kg.loc[idx, c], errors="coerce")
        diff = (a - b).abs()
        denom = b.abs().clip(lower=1.0)
        match = (diff <= (1e-6 + 1e-4 * denom)) | (a.isna() & b.isna())
        rows.append({
            "feature": c,
            "match_rate": float(match.mean()),
            "mean_abs_diff": float(diff.mean(skipna=True)),
            "corr": float(a.corr(b)) if a.notna().sum() > 2 else float("nan"),
        })
    return pd.DataFrame(rows).sort_values("match_rate")


def _split_and_eval(matrix, labels, feats, loo=False):
    """Out-of-time (by week) LR eval on a feature matrix. Returns (res, train, test)."""
    data = matrix[feats].copy()
    tw = pd.DataFrame({"t": [labels.get(str(i), (0, 0))[0] for i in matrix.index],
                       "w": [labels.get(str(i), (0, 0))[1] for i in matrix.index]},
                      index=matrix.index)
    data[TARGET] = tw["t"].astype(float).values
    data["week"] = tw["w"].astype(int).values
    data = data.dropna(subset=[TARGET]).sort_values("week")
    cut = int(len(data) * 0.8)
    train, test = data.iloc[:cut], data.iloc[cut:]
    res = fit_eval(train, test, feats, target=TARGET)
    return res, train, test


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="Step 3: real per-payment Kaggle -> open-banking round-trip.")
    ap.add_argument("kaggle_dir")
    ap.add_argument("--max-cases", type=int, default=25000)
    ap.add_argument("--batch", type=int, default=4000)
    ap.add_argument("--leave-one-out", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(args.kaggle_dir):
        print(f"ERROR: not a directory: {args.kaggle_dir}", file=sys.stderr)
        return 2

    max_cases = None if args.max_cases in (0, None) else args.max_cases

    # Build stamp -- makes any pasted run self-identifying so a stale extract
    # can never be mistaken for the current code.
    build_line = "BUILD ??? (VERSION.txt not found)"
    try:
        with open(os.path.join(_ROOT, "VERSION.txt"), "r", encoding="utf-8") as _vf:
            build_line = _vf.readline().strip()
    except OSError:
        pass

    print("=" * 78)
    print("STEP 3: reconstructing REAL per-payment Kaggle history via open banking")
    print(f">>> RUNNING: {build_line}")
    print("=" * 78)

    print("\n[1/5] streaming real Kaggle data -> canonical applicants (real per-payment DPD) ...")
    labels = read_labels(args.kaggle_dir, max_cases=max_cases)
    adapter = KaggleAdapter.from_parquet_dir(args.kaggle_dir, max_cases=max_cases)
    applicants = list(adapter.stream_canonical())
    print(f"      {len(applicants):,} applicants (no bucketing -- every payment keeps its reported DPD)")

    print("[2/5] converting to open-banking ground truth ...")
    pop = canonical_pop_to_ground_truth(applicants, labels=labels)

    print("[3/5] rendering (Kaggle + TrueLayer) and reconstructing via shared f() ...")
    ob, kg = reconstruct(pop, batch=args.batch)
    print(f"      OB matrix {ob.shape}   Kaggle-direct matrix {kg.shape}")

    print("\n[4/5] STEP 2 -- reconstruction fidelity (open banking vs Kaggle-direct)")
    rep = fidelity_report(ob, kg)
    with pd.option_context("display.width", 120, "display.max_rows", None):
        print(rep.to_string(index=False, float_format=lambda v: f"{v:0.4f}"))
    print(f"      mean match rate across parity features = {rep['match_rate'].mean():0.3f}")

    print("\n[5/5] STEP 1 -- logistic regression (out-of-time split by week)")
    feats = [c for c in REGISTRY.parity_names() if c in ob.columns and c in kg.columns]
    kg_res, _, _ = _split_and_eval(kg, labels, feats)
    ob_res, ob_tr, ob_te = _split_and_eval(ob, labels, feats)
    print(f"      Kaggle-direct (real feature power) : test Gini = {kg_res['test_gini']:0.4f}  ({verdict(kg_res['test_gini'])})")
    print(f"      Open-banking (fair reconstruction) : test Gini = {ob_res['test_gini']:0.4f}  ({verdict(ob_res['test_gini'])})")
    gap = kg_res["test_gini"] - ob_res["test_gini"]
    retained = (ob_res["test_gini"] / kg_res["test_gini"] * 100.0) if kg_res["test_gini"] > 0 else float("nan")
    print(f"      => open banking recovers {retained:0.1f}% of the Kaggle-direct Gini "
          f"(cost of reconstruction = {gap:0.4f})")

    if args.leave_one_out:
        print("\n      leave-one-out on the open-banking matrix (Gini lost when dropped):")
        for name, imp in leave_one_out(ob_tr, ob_te, feats, target=TARGET):
            print(f"        {name:32s} {imp:+0.4f}")

    print("\nDONE. Step 1 (model) + Step 2 (fidelity) complete on REAL per-payment data.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
