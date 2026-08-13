#!/usr/bin/env python3
"""
evidence_se.py -- standard errors and confidence intervals for the
open-banking credit scorecard.

-----------------------------------------------------------------------
WHAT AN "SE" MEANS HERE -- read this before quoting anything
-----------------------------------------------------------------------
Two separate objects come out of the same resampling loop, and NEITHER
assumes a normal distribution:

  SE  = the standard deviation of the replicate values.
        That is the DEFINITION of a standard error. No distribution is
        assumed to compute it.

  CI  = the 2.5th and 97.5th PERCENTILES of the replicate values.
        Also assumption-free.

Normality is only required to CONVERT one into the other:
     CI = point +/- 1.96*SE        (needs normality)
     SE = half-width / 1.96        (needs normality)
This script never does either. Both are read directly off the same 500
replicates, so they are mutually consistent without any distributional
assumption.

When the replicate distribution is skewed, the SE is a poor one-number
summary even though it is correctly computed. The script therefore
prints the SKEW alongside it. If |skew| > 0.5, quote the percentile
interval and treat the SE as descriptive only.

-----------------------------------------------------------------------
WHAT IT PRODUCES
-----------------------------------------------------------------------
  1. Gini point estimate + DeLong SE (parametric, i.i.d.)
  2. Gini block-bootstrap SE and CI, resampling whole origination weeks
  3. The DESIGN EFFECT = block SE / i.i.d. SE
  4. Per-vintage Gini and per-vintage default rate
  5. Bootstrap SE and CI on the calibrated PD curve (refits PAVA per
     replicate)
  6. Per-applicant score intervals at chosen reference scores
  7. Bootstrap distribution of the zero-default tail block -> floor CI

-----------------------------------------------------------------------
STATED ASSUMPTIONS  (reproduce these in the dissertation)
-----------------------------------------------------------------------
A1. The booster is HELD FIXED across replicates. Refitting LightGBM 500
    times is computationally infeasible, so the intervals reflect
    calibration- and evaluation-sampling variability but NOT model-
    fitting variability. Intervals are therefore somewhat too narrow.

A2. Features and hyperparameters are fixed. They were selected using
    this same data; model-selection uncertainty is not represented.

A3. The block bootstrap resamples whole origination weeks, so
    within-week clustering is preserved. It assumes weeks are mutually
    independent. Dependence with a horizon longer than one week is not
    captured. The effective sample size is the NUMBER OF WEEKS, not the
    number of rows.

A4. Resampling can only reproduce macroeconomic regimes that actually
    occurred in the observation window. Intervals are conditional on
    that window and contain no allowance for regime change.

A5. Right-censoring is propagated, not corrected. Later vintages have
    incomplete outcomes; resampling them reproduces that incompleteness.

A6. The ordinary n-out-of-n bootstrap is INCONSISTENT for cube-root-rate
    estimators at a point. Isotonic regression converges at n^(1/3) with
    a Chernoff limit, not n^(1/2) Gaussian. Therefore:
       - Gini and other smooth/averaged functionals: intervals are sound.
       - POINTWISE PD at a fixed score: approximate. Use --m-out-of-n to
         resample m = n^(2/3) blocks, which restores consistency.

A7. All intervals are reported as bootstrap PERCENTILES, never as
    mean +/- 1.96*SE. Tail PDs are bounded below by zero and strongly
    right-skewed; symmetric intervals would give negative lower bounds.

A8. The bootstrap refits a plain weighted PAVA on quantile-binned raw
    scores. The production calibrator additionally applies hybrid
    backbone extrapolation to ~0.2% of cases and fits unbinned. The
    difference is immaterial for interval width but the reconstruction
    is not bit-identical to production.
"""

from __future__ import annotations

import argparse
import json
import os
import time

import numpy as np
import pandas as pd

# --------------------------------------------------------------------
# scorecard constants -- must match obcredit/modeling/scorecard.py
# --------------------------------------------------------------------
DEFAULT_PDO = 40.0
DEFAULT_BASE_SCORE = 600.0
DEFAULT_BASE_ODDS = 20.0
BANDS = [(720.0, "A"), (660.0, "B"), (600.0, "C"), (540.0, "D")]

TARGET = "target"
WEEKCOL = "__week__"


def score_of_pd(p, pdo=DEFAULT_PDO, base_score=DEFAULT_BASE_SCORE,
                base_odds=DEFAULT_BASE_ODDS):
    """PD -> score. score = offset + factor * ln((1-p)/p)."""
    p = np.clip(np.asarray(p, dtype=float), 1e-12, 1.0 - 1e-12)
    factor = pdo / np.log(2.0)
    offset = base_score - factor * np.log(base_odds)
    return offset + factor * np.log((1.0 - p) / p)


def pd_of_score(s, pdo=DEFAULT_PDO, base_score=DEFAULT_BASE_SCORE,
                base_odds=DEFAULT_BASE_ODDS):
    """Inverse of score_of_pd."""
    factor = pdo / np.log(2.0)
    offset = base_score - factor * np.log(base_odds)
    z = (np.asarray(s, dtype=float) - offset) / factor
    return 1.0 / (1.0 + np.exp(z))


def band_of(score):
    for lo, name in BANDS:
        if score >= lo:
            return name
    return "E"


# --------------------------------------------------------------------
# ranking primitives
# --------------------------------------------------------------------
def rank_average(a):
    """Average (mid) ranks, fully vectorised. Ties share their mean rank."""
    a = np.asarray(a)
    order = np.argsort(a, kind="mergesort")
    s = a[order]
    n = a.size
    positions = np.arange(1, n + 1, dtype=float)
    new_group = np.r_[True, s[1:] != s[:-1]]
    grp = np.cumsum(new_group) - 1
    sums = np.bincount(grp, weights=positions)
    cnts = np.bincount(grp).astype(float)
    means = sums / cnts
    out = np.empty(n, dtype=float)
    out[order] = means[grp]
    return out


def auc_of(y, p):
    """AUC via the Mann-Whitney U statistic. Ties get half credit."""
    y = np.asarray(y, dtype=float)
    n1 = float((y == 1).sum())
    n0 = float((y == 0).sum())
    if n1 == 0.0 or n0 == 0.0:
        return float("nan")
    r = rank_average(np.asarray(p, dtype=float))
    return float((r[y == 1].sum() - n1 * (n1 + 1.0) / 2.0) / (n1 * n0))


def gini_of(y, p):
    return 2.0 * auc_of(y, p) - 1.0


def delong_var(y, p):
    """
    Fast DeLong (Sun & Xu 2014) variance of a single AUC.

    Fully NONPARAMETRIC: unlike the Hanley-McNeil closed form it makes no
    exponential assumption about the score distribution within each class.
    It does still assume independent observations -- which is exactly what
    the block bootstrap in this script is here to check.
    """
    y = np.asarray(y, dtype=float)
    p = np.asarray(p, dtype=float)
    x = p[y == 1]
    z = p[y == 0]
    m = x.size
    n = z.size
    if m == 0 or n == 0:
        return float("nan"), float("nan")

    tx = rank_average(x)
    tz = rank_average(z)
    txz = rank_average(np.concatenate([x, z]))

    auc = (txz[:m].sum() - m * (m + 1.0) / 2.0) / (m * n)

    v01 = (txz[:m] - tx) / n            # structural component, positives
    v10 = 1.0 - (txz[m:] - tz) / m      # structural component, negatives

    s01 = float(np.var(v01, ddof=1)) if m > 1 else 0.0
    s10 = float(np.var(v10, ddof=1)) if n > 1 else 0.0
    var = s01 / m + s10 / n
    return float(auc), float(var)


# --------------------------------------------------------------------
# isotonic regression (PAVA)
# --------------------------------------------------------------------
def pava(values, weights):
    """
    Pool-Adjacent-Violators. Returns the isotonic (non-decreasing) fit.

    Mean-preserving: sum(weights * fitted) == sum(weights * values).
    Solves the weighted least-squares isotonic problem exactly, and gives
    the same answer under any Bregman divergence (Barlow, Bartholomew,
    Bremner & Brunk 1972; Ayer et al. 1955).
    """
    v = np.asarray(values, dtype=float).copy()
    w = np.asarray(weights, dtype=float).copy()
    n = v.size
    if n == 0:
        return v

    lvl = np.empty(n, dtype=float)
    wt = np.empty(n, dtype=float)
    cnt = np.empty(n, dtype=np.int64)

    j = -1
    for i in range(n):
        j += 1
        lvl[j] = v[i]
        wt[j] = w[i]
        cnt[j] = 1
        # pool backwards while the sequence decreases
        while j > 0 and lvl[j - 1] > lvl[j]:
            tw = wt[j - 1] + wt[j]
            lvl[j - 1] = (lvl[j - 1] * wt[j - 1] + lvl[j] * wt[j]) / tw
            wt[j - 1] = tw
            cnt[j - 1] += cnt[j]
            j -= 1

    out = np.empty(n, dtype=float)
    pos = 0
    for b in range(j + 1):
        out[pos:pos + cnt[b]] = lvl[b]
        pos += cnt[b]
    return out


class BinnedIsotonic:
    """
    Weighted isotonic calibrator fitted on QUANTILE-BINNED raw scores.

    Why bin first?
      Production fits PAVA on every distinct raw score. On 305,332 rows
      that produced 161,166 knots -- 1.89 observations per knot. Each knot
      is then an estimate from ~2 people, which is the root cause of the
      wide tail interval. Binning into `nbins` quantile groups gives each
      knot ~150 observations instead, which is both far faster to refit
      (essential for bootstrapping) and statistically better behaved.

      Set nbins=0 to reproduce production exactly (slow: not recommended
      inside a bootstrap loop).
    """

    def __init__(self, nbins=2000, floor=0.0, ceiling=1.0):
        self.nbins = int(nbins)
        self.floor = float(floor)
        self.ceiling = float(ceiling)
        self.x_ = None
        self.y_ = None
        self.n_ = None
        self.k_ = None

    def fit(self, raw, y):
        raw = np.asarray(raw, dtype=float)
        y = np.asarray(y, dtype=float)

        if self.nbins and self.nbins > 1 and raw.size > self.nbins:
            qs = np.linspace(0.0, 1.0, self.nbins + 1)[1:-1]
            edges = np.unique(np.quantile(raw, qs))
            idx = np.searchsorted(edges, raw, side="right")
            ngrp = edges.size + 1
            cnt = np.bincount(idx, minlength=ngrp).astype(float)
            ksum = np.bincount(idx, weights=y, minlength=ngrp)
            xsum = np.bincount(idx, weights=raw, minlength=ngrp)
            keep = cnt > 0
            cnt = cnt[keep]
            ksum = ksum[keep]
            xc = xsum[keep] / cnt
        else:
            order = np.argsort(raw, kind="mergesort")
            rs = raw[order]
            ys = y[order]
            new_group = np.r_[True, rs[1:] != rs[:-1]]
            grp = np.cumsum(new_group) - 1
            cnt = np.bincount(grp).astype(float)
            ksum = np.bincount(grp, weights=ys)
            xc = rs[new_group]

        rate = ksum / cnt
        fitted = pava(rate, cnt)

        self.x_ = xc
        self.y_ = fitted
        self.n_ = cnt
        self.k_ = ksum
        return self

    def predict(self, raw):
        raw = np.asarray(raw, dtype=float)
        p = np.interp(raw, self.x_, self.y_,
                      left=self.y_[0], right=self.y_[-1])
        return np.clip(p, self.floor, self.ceiling)

    def tail_block(self):
        """(n_obs, n_defaults, n_knots) in the lowest fitted-PD block."""
        m = self.y_ <= self.y_.min() + 1e-12
        return (float(self.n_[m].sum()), float(self.k_[m].sum()), int(m.sum()))


def rule_of_three(n):
    return 3.0 / n if n > 0 else float("nan")


def beta_posterior_mean(n_obs, k_obs, base_rate, m):
    """Beta-Binomial posterior mean with a prior of strength m centred on
    the portfolio base rate."""
    a = m * base_rate + k_obs
    b = m * (1.0 - base_rate) + (n_obs - k_obs)
    return a / (a + b)


# --------------------------------------------------------------------
# data loading -- mirrors run_compare.three_way_split / _prep_lgbm
# --------------------------------------------------------------------
def three_way_split(m, f_train=0.6, f_calib=0.2):
    """Chronological split on __week__, identical to the training script."""
    sub = m.dropna(subset=[TARGET, WEEKCOL])
    sub = sub.sort_values(WEEKCOL, kind="mergesort")
    n = len(sub)
    i1 = int(n * f_train)
    i2 = int(n * (f_train + f_calib))
    ids = sub.index.to_numpy()
    return ids[:i1], ids[i1:i2], ids[i2:]


def load_everything(ob_path, artifacts):
    scorecard_path = os.path.join(artifacts, "scorecard.json")
    model_path = os.path.join(artifacts, "model_lgbm.txt")
    for pth in (ob_path, scorecard_path, model_path):
        if not os.path.exists(pth):
            raise SystemExit(f"[se] missing required file: {pth}")

    with open(scorecard_path, "r", encoding="utf-8") as fh:
        card = json.load(fh)

    print(f"[se] loading OB matrix: {ob_path}")
    m = pd.read_pickle(ob_path)
    print(f"[se] matrix rows={len(m):,} cols={m.shape[1]}")

    missing = [c for c in (TARGET, WEEKCOL) if c not in m.columns]
    if missing:
        raise SystemExit(f"[se] OB matrix missing {missing}")

    feats = list(card["features"])
    medians = card.get("medians", {}) or {}
    best = int(card.get("best_iteration") or 0) or None

    tr_ids, ca_ids, ev_ids = three_way_split(m)
    calib = m.loc[ca_ids]
    ev = m.loc[ev_ids]
    print(f"[se] split calib={len(calib):,} eval={len(ev):,}")

    import lightgbm as lgb
    booster = lgb.Booster(model_file=model_path)

    def prep(frame):
        X = frame.reindex(columns=feats)
        for c in feats:
            if c in medians and medians[c] is not None:
                X[c] = X[c].fillna(medians[c])
        return X.fillna(0.0).to_numpy(np.float32)

    # raw_score=True gives the log-odds margin, which is what the
    # production calibrator is fitted on.
    raw_ca = booster.predict(prep(calib), num_iteration=best, raw_score=True)
    raw_ev = booster.predict(prep(ev), num_iteration=best, raw_score=True)

    return {
        "card": card,
        "raw_ca": np.asarray(raw_ca, dtype=float),
        "raw_ev": np.asarray(raw_ev, dtype=float),
        "y_ca": calib[TARGET].astype(float).to_numpy(),
        "y_ev": ev[TARGET].astype(float).to_numpy(),
        "w_ca": calib[WEEKCOL].to_numpy(),
        "w_ev": ev[WEEKCOL].to_numpy(),
    }


# --------------------------------------------------------------------
# the single resampling loop
# --------------------------------------------------------------------
def block_indices(weeks, rng, m_out_of_n=False):
    """
    Resample WHOLE weeks with replacement and return row indices.

    Preserves within-week clustering. The effective sample size is the
    number of weeks, not the number of rows (assumption A3).

    m_out_of_n draws m = n_weeks^(2/3) blocks instead of n_weeks, which
    restores bootstrap consistency for cube-root-rate estimators such as
    pointwise isotonic values (assumption A6).
    """
    uniq, inv = np.unique(weeks, return_inverse=True)
    buckets = [np.flatnonzero(inv == i) for i in range(uniq.size)]
    nb = uniq.size
    draw = int(round(nb ** (2.0 / 3.0))) if m_out_of_n else nb
    draw = max(draw, 2)
    pick = rng.integers(0, nb, size=draw)
    return np.concatenate([buckets[i] for i in pick])


def iid_indices(n, rng):
    return rng.integers(0, n, size=n)


def _sd(a):
    """
    A genuine bootstrap standard error: the standard deviation of the
    replicate values.

    This is the DEFINITION of a standard error, not a distributional
    assumption. Nothing here supposes normality. Normality would only be
    needed to convert this SE into an interval (point +/- 1.96*SE), or an
    interval back into an SE (half-width / 1.96). We never do either --
    the intervals are read off as percentiles of the same replicates.
    """
    a = np.asarray(a, dtype=float)
    a = a[np.isfinite(a)]
    return float(np.std(a, ddof=1)) if a.size > 1 else float("nan")


def _skew(a):
    """
    Fisher-Pearson skew of the replicates.

    |skew| > ~0.5 means the replicate distribution is materially
    asymmetric, so the SE -- though correctly computed -- is a poor
    one-number summary. Quote the percentile interval instead.
    """
    a = np.asarray(a, dtype=float)
    a = a[np.isfinite(a)]
    if a.size < 3:
        return float("nan")
    c = a - a.mean()
    s = float(np.sqrt((c ** 2).mean()))
    return float((c ** 3).mean() / s ** 3) if s > 0 else float("nan")


def _pct(a, lo=2.5, hi=97.5):
    a = np.asarray(a, dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return (float("nan"), float("nan"))
    return (float(np.percentile(a, lo)), float(np.percentile(a, hi)))


def run_bootstrap(data, n_boot=500, nbins=2000, floor=0.0075,
                  ref_scores=(600.0, 660.0, 709.0), seed=42,
                  m_out_of_n=False, do_iid=True, prior_m=100.0):
    raw_ca = data["raw_ca"]
    raw_ev = data["raw_ev"]
    y_ca = data["y_ca"]
    y_ev = data["y_ev"]
    w_ca = data["w_ca"]
    w_ev = data["w_ev"]

    rng = np.random.default_rng(seed)

    # ---- point estimates on the real data --------------------------
    cal0 = BinnedIsotonic(nbins=nbins, floor=floor,
                          ceiling=1.0 - floor).fit(raw_ca, y_ca)
    gini0 = gini_of(y_ev, raw_ev)
    auc0, var0 = delong_var(y_ev, raw_ev)
    se_gini_delong = 2.0 * np.sqrt(var0)
    n_ta, k_ta, kn_ta = cal0.tail_block()
    base_rate_ca = float(y_ca.mean())

    # locate the raw margin corresponding to each reference score
    ref_scores = tuple(float(s) for s in ref_scores)
    ref_raw = []
    for s in ref_scores:
        target_pd = pd_of_score(s)
        j = int(np.searchsorted(cal0.y_, target_pd, side="left"))
        j = min(max(j, 0), cal0.x_.size - 1)
        ref_raw.append(cal0.x_[j])
    ref_raw = np.asarray(ref_raw, dtype=float)
    ref_pd0 = cal0.predict(ref_raw)

    # ---- replicate storage -----------------------------------------
    g_block = np.full(n_boot, np.nan)
    g_iid = np.full(n_boot, np.nan)
    pd_ref = np.full((n_boot, len(ref_scores)), np.nan)
    sc_ref = np.full((n_boot, len(ref_scores)), np.nan)
    mean_pd = np.full(n_boot, np.nan)
    tail_n = np.full(n_boot, np.nan)
    floor_b = np.full(n_boot, np.nan)

    t0 = time.time()
    for b in range(n_boot):
        # --- block resample of the EVAL slice -> ranking uncertainty
        ie = block_indices(w_ev, rng, m_out_of_n=m_out_of_n)
        g_block[b] = gini_of(y_ev[ie], raw_ev[ie])

        # --- block resample of the CALIB slice -> refit the calibrator
        ic = block_indices(w_ca, rng, m_out_of_n=m_out_of_n)
        calb = BinnedIsotonic(nbins=nbins, floor=floor,
                              ceiling=1.0 - floor).fit(raw_ca[ic], y_ca[ic])

        pr = calb.predict(ref_raw)
        pd_ref[b, :] = pr
        sc_ref[b, :] = score_of_pd(pr)
        mean_pd[b] = float(calb.predict(raw_ev[ie]).mean())

        tn, tk, _ = calb.tail_block()
        tail_n[b] = tn
        floor_b[b] = beta_posterior_mean(tn, tk, float(y_ca[ic].mean()),
                                         prior_m)

        # --- i.i.d. resample, for the design effect only
        if do_iid:
            ii = iid_indices(y_ev.size, rng)
            g_iid[b] = gini_of(y_ev[ii], raw_ev[ii])

        if (b + 1) % 50 == 0:
            el = time.time() - t0
            rate = (b + 1) / el
            print(f"[se] {b + 1}/{n_boot} replicates "
                  f"({rate:0.1f}/s, eta {(n_boot - b - 1) / rate:0.0f}s)",
                  flush=True)

    se_block = _sd(g_block)
    se_iid = _sd(g_iid) if do_iid else float("nan")
    deff = se_block / se_iid if do_iid and se_iid > 0 else float("nan")

    nref = len(ref_scores)
    return {
        "n_eval": int(y_ev.size),
        "n_calib": int(y_ca.size),
        "n_weeks_eval": int(np.unique(w_ev).size),
        "n_weeks_calib": int(np.unique(w_ca).size),
        "base_rate_calib": base_rate_ca,
        "base_rate_eval": float(y_ev.mean()),
        "gini_point": float(gini0),
        "auc_point": float(auc0),
        "se_gini_delong": float(se_gini_delong),
        "se_gini_block": se_block,
        "se_gini_iid_boot": se_iid,
        "skew_gini_block": _skew(g_block),
        "design_effect": deff,
        "gini_ci_block": _pct(g_block),
        "gini_ci_iid": _pct(g_iid) if do_iid else None,
        "gini_ci_delong": (float(gini0 - 1.96 * se_gini_delong),
                           float(gini0 + 1.96 * se_gini_delong)),
        "tail_n_point": n_ta,
        "tail_k_point": k_ta,
        "tail_knots_point": kn_ta,
        "tail_n_ci": _pct(tail_n),
        "se_tail_n": _sd(tail_n),
        "rule_of_three": rule_of_three(n_ta),
        "floor_point": beta_posterior_mean(n_ta, k_ta, base_rate_ca, prior_m),
        "floor_ci": _pct(floor_b),
        "se_floor": _sd(floor_b),
        "skew_floor": _skew(floor_b),
        "mean_pd_point": float(cal0.predict(raw_ev).mean()),
        "mean_pd_ci": _pct(mean_pd),
        "se_mean_pd": _sd(mean_pd),
        "skew_mean_pd": _skew(mean_pd),
        "ref_scores": list(ref_scores),
        "ref_pd_point": [float(v) for v in ref_pd0],
        "ref_pd_ci": [_pct(pd_ref[:, j]) for j in range(nref)],
        "se_ref_pd": [_sd(pd_ref[:, j]) for j in range(nref)],
        "skew_ref_pd": [_skew(pd_ref[:, j]) for j in range(nref)],
        "ref_score_ci": [_pct(sc_ref[:, j]) for j in range(nref)],
        "se_ref_score": [_sd(sc_ref[:, j]) for j in range(nref)],
        "skew_ref_score": [_skew(sc_ref[:, j]) for j in range(nref)],
        "floor_clipped_frac": [float(np.mean(pd_ref[:, j] <= floor + 1e-12))
                               for j in range(nref)],
        "n_boot": int(n_boot),
        "nbins": int(nbins),
        "floor_used": float(floor),
        "m_out_of_n": bool(m_out_of_n),
    }


def vintage_table(data, min_each=200):
    """Per-vintage Gini and default rate. The censoring diagnostic."""
    w = data["w_ev"]
    y = data["y_ev"]
    p = data["raw_ev"]
    rows = []
    for v in np.unique(w):
        msk = w == v
        if msk.sum() < min_each:
            continue
        yy = y[msk]
        if yy.sum() < 5 or (yy == 0).sum() < 5:
            continue
        rows.append({
            "vintage": float(v),
            "n": int(msk.sum()),
            "defaults": int(yy.sum()),
            "default_rate": float(yy.mean()),
            "gini": float(gini_of(yy, p[msk])),
        })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------
def fmt_ci(ci, dp=4):
    return f"[{ci[0]:.{dp}f}, {ci[1]:.{dp}f}]"


def report(res, vt):
    print()
    print("=" * 72)
    print("  EVIDENCE / STANDARD ERRORS")
    print("=" * 72)
    print(f"eval rows          : {res['n_eval']:,}  "
          f"({res['n_weeks_eval']} origination weeks)")
    print(f"calib rows         : {res['n_calib']:,}  "
          f"({res['n_weeks_calib']} origination weeks)")
    print(f"base rate calib    : {res['base_rate_calib']:.6f}")
    print(f"base rate eval     : {res['base_rate_eval']:.6f}")
    print(f"replicates         : {res['n_boot']}   bins={res['nbins']}   "
          f"floor={res['floor_used']}   m_out_of_n={res['m_out_of_n']}")
    print()
    print("NOTE ON SEs. Every SE below is the standard deviation of the")
    print("bootstrap replicates -- that is the definition of a standard")
    print("error and assumes no distribution. Every CI below is a pair of")
    print("percentiles of those same replicates, and also assumes no")
    print("distribution. They are NOT converted into one another: doing")
    print("that (point +/- 1.96*SE) would require normality, which does")
    print("not hold here. Where |skew| > 0.5 the SE is descriptive only")
    print("and the interval is the number to quote.")

    print()
    print("--- 1. DISCRIMINATION (ranking) -----------------------------------")
    print(f"Gini (point)              : {res['gini_point']:.4f}")
    print(f"AUC  (point)              : {res['auc_point']:.4f}")
    print(f"SE, DeLong (i.i.d.)       : {res['se_gini_delong']:.4f}")
    print(f"SE, i.i.d. bootstrap      : {res['se_gini_iid_boot']:.4f}")
    print(f"SE, BLOCK bootstrap       : {res['se_gini_block']:.4f}"
          f"   (skew {res['skew_gini_block']:+.2f})")
    print(f"DESIGN EFFECT             : {res['design_effect']:.2f}x")
    print(f"95% CI, DeLong            : {fmt_ci(res['gini_ci_delong'])}")
    print(f"95% CI, BLOCK bootstrap   : {fmt_ci(res['gini_ci_block'])}")
    de = res["design_effect"]
    if np.isfinite(de) and de > 1.5:
        print(f"  >> DESIGN EFFECT > 1.5. Variance inflation is "
              f"{de ** 2:.2f}x, so the")
        print(f"     effective sample size is about "
              f"{int(res['n_eval'] / de ** 2):,} not {res['n_eval']:,}.")
        print("     Quote the BLOCK interval. DeLong and the i.i.d.")
        print("     bootstrap are both too narrow.")
    elif np.isfinite(de):
        print("  >> Design effect modest; i.i.d. SEs roughly adequate.")
        print("     Quote the block interval as the headline anyway.")

    print()
    print("--- 2. THE TAIL BLOCK AND THE FLOOR -------------------------------")
    print(f"tail block observations   : {res['tail_n_point']:.0f}")
    print(f"tail block defaults       : {res['tail_k_point']:.0f}")
    print(f"tail block knots          : {res['tail_knots_point']}")
    print(f"tail n: SE {res['se_tail_n']:.1f}   "
          f"95% CI {fmt_ci(res['tail_n_ci'], 0)}")
    print(f"rule of three  (3/n)      : {res['rule_of_three']:.5f}"
          f"  -> score {score_of_pd(res['rule_of_three']):.1f}")
    print(f"Beta posterior mean       : {res['floor_point']:.5f}"
          f"  -> score {score_of_pd(res['floor_point']):.1f}")
    print(f"floor: SE {res['se_floor']:.5f}   "
          f"95% CI {fmt_ci(res['floor_ci'], 5)}   "
          f"skew {res['skew_floor']:+.2f}")
    lo, hi = res["floor_ci"]
    if np.isfinite(lo) and np.isfinite(hi):
        print(f"  implied ceiling range     : "
              f"{score_of_pd(hi):.1f} .. {score_of_pd(lo):.1f}")

    print()
    print("--- 3. PD AND SCORE: SEs AND INTERVALS ----------------------------")
    print(f"mean PD on eval           : {res['mean_pd_point']:.5f}")
    print(f"  SE {res['se_mean_pd']:.5f}   "
          f"95% CI {fmt_ci(res['mean_pd_ci'], 5)}   "
          f"skew {res['skew_mean_pd']:+.2f}")
    print(f"  observed eval rate      : {res['base_rate_eval']:.5f}")
    mlo, mhi = res["mean_pd_ci"]
    if mlo <= res["base_rate_eval"] <= mhi:
        print("  >> The observed rate lies INSIDE the interval: no")
        print("     detectable calibration-in-the-large error.")
    else:
        print("  >> The observed rate lies OUTSIDE the interval: there IS")
        print("     a detectable calibration-in-the-large error.")

    print()
    print(f"{'score':>6} {'PD':>9} {'SE(PD)':>9} {'skew':>6} "
          f"{'PD 95% CI':>21} {'SE(scr)':>8} {'score 95% CI':>17}  band")
    for j, s in enumerate(res["ref_scores"]):
        p0 = res["ref_pd_point"][j]
        sep = res["se_ref_pd"][j]
        skp = res["skew_ref_pd"][j]
        plo, phi = res["ref_pd_ci"][j]
        ses = res["se_ref_score"][j]
        slo, shi = res["ref_score_ci"][j]
        bl = band_of(slo)
        bh = band_of(shi)
        bstr = bh if bl == bh else f"{bl}-{bh}"
        clip = res["floor_clipped_frac"][j]
        flag = " *" if clip > 0.01 else ""
        print(f"{s:>6.0f} {p0:>9.5f} {sep:>9.5f} {skp:>+6.2f} "
              f"[{plo:>8.5f},{phi:>8.5f}] {ses:>8.2f} "
              f"[{slo:>7.1f},{shi:>7.1f}]  {bstr}{flag}")
    if any(c > 0.01 for c in res["floor_clipped_frac"]):
        print()
        print("  * this row hit the PD floor in more than 1% of replicates.")
        print("    Its lower PD bound is CENSORED by the floor, not")
        print("    estimated. Report it as '<= floor', and treat its SE as")
        print("    an understatement of the true uncertainty.")
    print()
    print("  The interval is VERTICAL. The applicant's position in the")
    print("  ranking is fixed by the booster; what moves across replicates")
    print("  is the PD attached to that position.")

    print()
    print("--- 4. VINTAGE ANALYSIS (censoring diagnostic) --------------------")
    if vt is None or vt.empty:
        print("  not enough rows per vintage to report")
    else:
        rate = vt["default_rate"].to_numpy()
        gg = vt["gini"].to_numpy()
        print(f"vintages reported         : {len(vt)}")
        print(f"default rate  min/med/max : "
              f"{rate.min():.4f} / {np.median(rate):.4f} / {rate.max():.4f}")
        print(f"gini          min/med/max : "
              f"{gg.min():.4f} / {np.median(gg):.4f} / {gg.max():.4f}")
        first = rate[:max(1, len(rate) // 3)].mean()
        last = rate[-max(1, len(rate) // 3):].mean()
        print(f"early-third mean rate     : {first:.4f}")
        print(f"late-third  mean rate     : {last:.4f}")
        if last > 0 and first / last > 1.25:
            print("  >> Default rate DECLINES sharply across vintages.")
            print("     Consistent with RIGHT-CENSORING: late vintages have")
            print("     not had time to default. Do NOT recentre central")
            print("     tendency on the eval rate -- it is an undercount.")
        else:
            print("  >> Default rate roughly stable across vintages, so the")
            print("     calib/eval gap is NOT mainly censoring. A central")
            print("     tendency target below the calib rate is defensible.")

    print()
    print("=" * 72)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Standard errors for ranking and PD.")
    ap.add_argument("--ob", required=True, help="path to the OB matrix pickle")
    ap.add_argument("--artifacts", default="artifacts",
                    help="folder holding scorecard.json and model_lgbm.txt")
    ap.add_argument("--boot", type=int, default=500,
                    help="bootstrap replicates (default 500)")
    ap.add_argument("--bins", type=int, default=2000,
                    help="quantile bins for the refit; 0 = exact/slow")
    ap.add_argument("--floor", type=float, default=0.0075,
                    help="PD floor applied in the reconstruction")
    ap.add_argument("--prior-m", type=float, default=100.0,
                    help="Beta prior strength for the floor posterior")
    ap.add_argument("--scores", default="600,660,709",
                    help="comma-separated reference scores")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--m-out-of-n", action="store_true",
                    help="draw n^(2/3) blocks; consistent for pointwise "
                         "isotonic values (assumption A6)")
    ap.add_argument("--no-iid", action="store_true",
                    help="skip the i.i.d. bootstrap (no design effect)")
    ap.add_argument("--min-per-vintage", type=int, default=200)
    ap.add_argument("--json-out", default=None,
                    help="write the full result dict to this path")
    ap.add_argument("--vintage-csv", default=None)
    args = ap.parse_args(argv)

    refs = tuple(float(s) for s in args.scores.split(",") if s.strip())

    data = load_everything(args.ob, args.artifacts)
    res = run_bootstrap(
        data,
        n_boot=args.boot,
        nbins=args.bins,
        floor=args.floor,
        ref_scores=refs,
        seed=args.seed,
        m_out_of_n=args.m_out_of_n,
        do_iid=not args.no_iid,
        prior_m=args.prior_m,
    )
    vt = vintage_table(data, min_each=args.min_per_vintage)

    report(res, vt)

    if args.json_out:
        d = os.path.dirname(os.path.abspath(args.json_out))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=2, default=float)
        print(f"[se] wrote {args.json_out}")
    if args.vintage_csv and vt is not None and not vt.empty:
        vt.to_csv(args.vintage_csv, index=False)
        print(f"[se] wrote {args.vintage_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
