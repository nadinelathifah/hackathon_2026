"""STEP 3 tests -- prove that Kaggle data reshaped into the open-banking format
reconstructs the SAME features (step 2) and trains a real model (step 1).

These are pytest-compatible AND runnable by ../run_tests.py without pytest.
Each returns None on success and raises AssertionError on failure. Everything is
in-memory, so it runs in any environment (no parquet / sklearn / xgboost).

What is proven here:
  A. round-trip parity -- for a population with on-time, MISSED and small-LATE
     cycles, the open-banking-reconstructed parity features equal the
     Kaggle-direct features (the mechanism is source-agnostic).
  B. delinquency ordering -- a missed/late payer looks riskier than a clean one
     on BOTH renderings.
  C. model signal -- a logistic regression trained on the OPEN-BANKING-
     reconstructed features recovers strong signal when risk is delinquency-
     driven (Gini well above the REAL threshold).
  D. synthesis -- the Kaggle-summary -> ground-truth reverse map obeys its rules
     (serious arrears -> missed cycles; >=3 payments kept present).
"""
from __future__ import annotations
import math
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from obcredit.adapters import KaggleAdapter, TrueLayerAdapter  # noqa: E402
from obcredit.config import DEFAULT  # noqa: E402
from obcredit.feature_registry import REGISTRY  # noqa: E402
from obcredit.pipeline import FeaturePipeline  # noqa: E402

from obcredit.canonical import (CanonicalApplicant, CanonicalObligation,  # noqa: E402
                                CanonicalPayment)

from step3lib.ground_truth import GTApplicant, GTObligation  # noqa: E402
from step3lib.renderers import to_kaggle_frames, to_truelayer_payloads  # noqa: E402
from step3lib.kaggle_stream import (synthesize_applicant, summarize_to_applicants,  # noqa: E402
                                    canonical_to_ground_truth)
from step3lib.model import fit_eval, verdict  # noqa: E402

AS_OF = date(2024, 6, 1)


def _clean_ob(case_id, income=2600.0, target=0, week=0):
    """12 monthly cycles, all on time."""
    return GTApplicant(
        case_id=case_id, as_of=AS_OF, monthly_income=income, target=target, week=week,
        obligations=[GTObligation("ACME LOAN", 300.0, date(2023, 7, 1), 11, 30)],
    )


def _arrears_ob(case_id, income=2600.0, target=1, week=0):
    """Same schedule but with two missed cycles and one small-late cycle."""
    return GTApplicant(
        case_id=case_id, as_of=AS_OF, monthly_income=income, target=target, week=week,
        obligations=[GTObligation("ACME LOAN", 300.0, date(2023, 7, 1), 11, 30,
                                  missed=(3, 7), late={5: 9})],
    )


def _matrices(pop):
    pipe = FeaturePipeline()
    k = pipe.build_matrix(KaggleAdapter(to_kaggle_frames(pop)).to_canonical())
    t = pipe.build_matrix(TrueLayerAdapter(to_truelayer_payloads(pop)).to_canonical())
    return k, t


def _close(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, float) and math.isnan(a) and isinstance(b, float) and math.isnan(b):
        return True
    return abs(float(a) - float(b)) <= max(
        DEFAULT.parity_abs_tol, DEFAULT.parity_rel_tol * max(abs(float(a)), abs(float(b))))


# --------------------------------------------------------------------- A
def test_roundtrip_parity_features_match():
    pop = [_clean_ob("3001"), _arrears_ob("3002")]
    k, t = _matrices(pop)
    assert list(k.index) == list(t.index), (list(k.index), list(t.index))
    mismatches = []
    for case_id in k.index:
        for col in REGISTRY.parity_names():
            if col not in k.columns or col not in t.columns:
                continue
            a, b = k.loc[case_id, col], t.loc[case_id, col]
            if not _close(a, b):
                mismatches.append((case_id, col, a, b))
    assert not mismatches, "PARITY MISMATCH:\n" + "\n".join(
        f"  case={c} feature={f}: kaggle={a} truelayer={b}" for c, f, a, b in mismatches)


# --------------------------------------------------------------------- B
def test_arrears_payer_is_riskier_both_sources():
    pop = [_clean_ob("3001"), _arrears_ob("3002")]
    k, t = _matrices(pop)
    for m in (k, t):
        assert m.loc["3002", "total_overdue_amount"] > m.loc["3001", "total_overdue_amount"]
        assert m.loc["3002", "num_dpd_events_24m"] >= m.loc["3001", "num_dpd_events_24m"]
        assert m.loc["3002", "max_dpd_24m"] >= m.loc["3001", "max_dpd_24m"]


# --------------------------------------------------------------------- C
def test_logreg_recovers_signal_from_open_banking():
    pop = []
    for i in range(30):
        pop.append(_clean_ob(f"g{i:03d}", income=2400 + 10 * i, target=0, week=i % 5))
    for i in range(30):
        # vary the arrears a little so features aren't degenerate
        miss = (3, 7) if i % 2 == 0 else (2, 6, 9)
        ob = GTApplicant(
            case_id=f"b{i:03d}", as_of=AS_OF, monthly_income=2300 + 10 * i,
            target=1, week=i % 5,
            obligations=[GTObligation("ACME LOAN", 320.0, date(2023, 7, 1), 11, 30,
                                      missed=miss, late={5: 8})],
        )
        pop.append(ob)
    pipe = FeaturePipeline()
    ob_matrix = pipe.build_matrix(TrueLayerAdapter(to_truelayer_payloads(pop)).to_canonical())
    feats = [c for c in REGISTRY.parity_names() if c in ob_matrix.columns]
    tgt = {a.case_id: a.target for a in pop}
    data = ob_matrix.copy()
    data["target"] = [tgt[c] for c in data.index]
    # simple split: interleave so both classes appear in train and test
    train = data.iloc[::2]
    test = data.iloc[1::2]
    res = fit_eval(train, test, feats, target="target")
    assert res["test_gini"] > 0.40, (res["test_gini"], verdict(res["test_gini"]))


# --------------------------------------------------------------------- D
def test_synthesis_rules():
    row = {
        "num_bureau_payments": 10, "num_dpd_gt0": 5, "num_dpd_ge30": 3,
        "max_dpd": 90, "mean_dpd": 30, "max_overdue": 300, "total_overdue": 900,
        "total_annuity": 600, "max_annuity": 300, "num_prev_apps": 2,
        "monthly_income": 2500, "as_of": "2024-06-01", "target": 1, "week": 4,
    }
    app = synthesize_applicant("9001", row)
    assert app.target == 1 and app.week == 4
    ob = app.obligations[0]
    # serious arrears (3) become missed cycles; >=3 payments kept present
    assert len(ob.missed) == 3
    assert ob.n_present() >= 3
    # mild-late count = gt0 - ge30 = 2
    assert len(ob.late) == 2
    # and the synthesised case round-trips through both renderers without error
    k, t = _matrices([app])
    assert list(k.index) == list(t.index) == ["9001"]


def test_summarize_to_applicants_shapes():
    summary = pd.DataFrame(
        {
            "num_bureau_payments": [0, 8],
            "num_dpd_gt0": [0, 2], "num_dpd_ge30": [0, 1],
            "max_dpd": [0, 90], "mean_dpd": [0, 20],
            "max_overdue": [0, 300], "total_overdue": [0, 300],
            "total_annuity": [0, 250], "max_annuity": [0, 250], "num_prev_apps": [0, 1],
            "monthly_income": [0, 2000],
            "as_of": ["2024-06-01", "2024-06-01"], "target": [0, 1], "week": [1, 2],
        },
        index=["c0", "c1"],
    )
    apps = summarize_to_applicants(summary)
    assert [a.case_id for a in apps] == ["c0", "c1"]
    assert apps[0].obligations == []           # no bureau payments -> no obligation
    assert apps[1].obligations[0].n_payments == 8


# --------------------------------------------------------------------- E
def test_real_per_cycle_roundtrip_parity():
    """The REAL per-payment path (dpd_seq, no bucketing): an obligation whose
    cycles carry their own reported DPD -- on-time, small-late (<15d, recovered
    by timing) and serious (>=15d, rendered as a missed direct debit) -- must
    reconstruct identically from Kaggle bureau and open banking."""
    dpd_seq = (0, 0, 0, 6, 0, 0, 90, 0, 9, 0, 0)          # one serious + two mild
    ovd_seq = (0, 0, 0, 0, 0, 0, 300, 0, 0, 0, 0)
    clean = (0,) * 11
    pop = [
        GTApplicant("5001", AS_OF, 2600.0, target=0, week=0,
                    obligations=[GTObligation("OBLIG 5001-0", 300.0, date(2023, 7, 1),
                                              11, 30, dpd_seq=clean, overdue_seq=(0,) * 11)]),
        GTApplicant("5002", AS_OF, 2600.0, target=1, week=0,
                    obligations=[GTObligation("OBLIG 5002-0", 300.0, date(2023, 7, 1),
                                              11, 30, dpd_seq=dpd_seq, overdue_seq=ovd_seq)]),
    ]
    k, t = _matrices(pop)
    assert list(k.index) == list(t.index)
    mismatches = []
    for case_id in k.index:
        for col in REGISTRY.parity_names():
            if col not in k.columns or col not in t.columns:
                continue
            a, b = k.loc[case_id, col], t.loc[case_id, col]
            if not _close(a, b):
                mismatches.append((case_id, col, a, b))
    assert not mismatches, "REAL-PATH PARITY MISMATCH:\n" + "\n".join(
        f"  case={c} feature={f}: kaggle={a} truelayer={b}" for c, f, a, b in mismatches)
    # the arrears case must be reconstructed as riskier on BOTH sides
    for m in (k, t):
        assert m.loc["5002", "max_dpd_24m"] >= m.loc["5001", "max_dpd_24m"]
        assert m.loc["5002", "num_serious_arrears_24m"] >= 1


# --------------------------------------------------------------------- F
def test_canonical_to_ground_truth_carries_real_dpd():
    """canonical_to_ground_truth must copy every payment's reported DPD/overdue
    verbatim (no bucketing) and keep the most recent MAX_CYCLES."""
    pays = [CanonicalPayment("o1", date(2023, 7 + (i // 28), 1 + (i % 28)), 0.0,
                             overdue=(300.0 if d >= 90 else 0.0), dpd=float(d))
            for i, d in enumerate([0, 0, 5, 0, 30, 0, 90, 0, 7, 0])]
    ob = CanonicalObligation(obligation_id="o1", kind="loan",
                             opened=date(2023, 7, 1), payments=pays)
    app = CanonicalApplicant(case_id="7001", as_of=AS_OF,
                             obligations=[ob], accounts=[],
                             monthly_income=2500.0, instalments=[250.0, 350.0],
                             declared={})
    gt = canonical_to_ground_truth(app, target=1, week=3)
    assert gt.case_id == "7001" and gt.target == 1 and gt.week == 3
    gob = gt.obligations[0]
    # DPD sequence carried through unchanged (order preserved by date)
    assert list(gob.dpd_seq) == [0, 0, 5, 0, 30, 0, 90, 0, 7, 0]
    assert gob.overdue_seq[6] == 300.0
    # representative instalment = median of applprev annuities
    assert abs(gob.instalment - 300.0) < 1e-9
    # round-trips through both renderers
    k, t = _matrices([gt])
    assert list(k.index) == list(t.index) == ["7001"]


# --------------------------------------------------------------------- G
def test_multi_obligation_identity_roundtrip():
    """REGRESSION: several DISTINCT obligations on ONE applicant must survive as
    separate open-banking streams, not be merged into one.

    Before the payee-naming fix the counterparty normaliser stripped the digits
    from 'OBLIG <case>-<idx>', collapsing every credit line of an applicant to a
    single key. The recurring-stream detector then merged all lines into ONE
    open-banking obligation, so num_active_obligations / total_annuity and the
    per-line DPD schedule diverged from the Kaggle-direct view -- exactly the
    failure seen on the 25k real run (num_active_obligations corr ~0.02).
    """
    # All three share the SAME instalment AND the same schedule on purpose: with
    # identical amounts the ONLY thing that can keep them as separate open-
    # banking streams is a distinct payee name. If the payee-naming fix
    # regresses, the digit-stripped counterparty collapses all three into ONE
    # stream and num_active_obligations drops to 1 -- failing this test. (Using
    # different amounts here would let the detector separate them by amount and
    # silently hide a naming regression, which is exactly what happened before.)
    obs = [
        GTObligation("OBLIG 8001-0", 300.0, date(2023, 7, 1), 10, 30,
                     dpd_seq=(0,) * 10, overdue_seq=(0,) * 10),
        GTObligation("OBLIG 8001-1", 300.0, date(2023, 7, 1), 10, 30,
                     dpd_seq=(0, 0, 0, 6, 0, 0, 0, 9, 0, 0), overdue_seq=(0,) * 10),
        GTObligation("OBLIG 8001-2", 300.0, date(2023, 7, 1), 10, 30,
                     dpd_seq=(0,) * 10, overdue_seq=(0,) * 10),
    ]
    pop = [GTApplicant("8001", AS_OF, 5000.0, target=0, week=0, obligations=obs)]
    k, t = _matrices(pop)
    # BOTH sources must see THREE obligations, not one merged stream
    assert k.loc["8001", "num_active_obligations"] == 3.0, k.loc["8001", "num_active_obligations"]
    assert t.loc["8001", "num_active_obligations"] == 3.0, t.loc["8001", "num_active_obligations"]
    # total annuity = 200+300+400 = 900 on both sides
    assert _close(k.loc["8001", "total_annuity"], 900.0)
    assert _close(t.loc["8001", "total_annuity"], 900.0)
    # every parity feature must match across the two renderings
    mismatches = []
    for col in REGISTRY.parity_names():
        if col in k.columns and col in t.columns:
            a, b = k.loc["8001", col], t.loc["8001", col]
            if not _close(a, b):
                mismatches.append((col, a, b))
    assert not mismatches, "MULTI-OB PARITY MISMATCH:\n" + "\n".join(
        f"  feature={f}: kaggle={a} truelayer={b}" for f, a, b in mismatches)


ALL_TESTS = [
    test_roundtrip_parity_features_match,
    test_arrears_payer_is_riskier_both_sources,
    test_logreg_recovers_signal_from_open_banking,
    test_synthesis_rules,
    test_summarize_to_applicants_shapes,
    test_real_per_cycle_roundtrip_parity,
    test_canonical_to_ground_truth_carries_real_dpd,
    test_multi_obligation_identity_roundtrip,
]
