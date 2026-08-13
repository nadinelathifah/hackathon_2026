"""Isotonic calibration (pure-NumPy PAVA) + calibration diagnostics.

Maps a raw model score to a monotone-increasing calibrated probability of
default. Isotonic regression only assumes "higher score -> higher-or-equal
default rate", so it PRESERVES the ranking (and therefore Gini/AUC) while
turning the score into a genuine probability. No scikit-learn dependency, so it
runs in the same lean environment as the rest of the project.

BUILD 18
--------
BUILD 17 only rewrote the curve OUTSIDE the observed score support, so it never
touched the real problem: PAVA pools the low-score end of the calibration set
into a FLAT PLATEAU (in this project at ~5-6% PD, because OB->OB Gini is only
~0.41). Everything inside that plateau gets the same PD, so the credit score is
hard-capped around 590 no matter how strong the applicant is.

BUILD 18 adds tail="hybrid", which may also replace a TERMINAL PLATEAU that sits
inside the support -- but only when there is evidence the plateau is a pooling
artefact rather than a real flat region of risk. It also adds the three things
an IRB PD model needs once you start extrapolating: a regulatory PD floor, a
Margin of Conservatism, and a central-tendency re-anchor.
"""
from __future__ import annotations
import pickle
from typing import List, Optional, Tuple

import numpy as np

# CRR Art. 160/163 impose a 0.03% (3bp) regulatory floor on PD estimates.
# BUILD <=17 defaulted to 1e-4 (1bp), which is BELOW the regulatory floor.
BASEL_PD_FLOOR = 0.0003


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -700.0, 700.0)))


def _pava(y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Pool-Adjacent-Violators: non-decreasing weighted least-squares fit of y.

    Stack-based, O(n). Adjacent blocks that violate monotonicity are pooled into
    their weighted mean until the whole sequence is non-decreasing.
    """
    n = len(y)
    vals: List[float] = []
    weights: List[float] = []
    counts: List[int] = []
    for i in range(n):
        vals.append(float(y[i]))
        weights.append(float(w[i]))
        counts.append(1)
        while len(vals) > 1 and vals[-2] > vals[-1]:
            w2 = weights[-1] + weights[-2]
            v2 = (vals[-1] * weights[-1] + vals[-2] * weights[-2]) / w2
            c2 = counts[-1] + counts[-2]
            vals.pop(); weights.pop(); counts.pop()
            vals[-1] = v2; weights[-1] = w2; counts[-1] = c2
    out = np.empty(n, dtype=float)
    idx = 0
    for v, c in zip(vals, counts):
        out[idx:idx + c] = v
        idx += c
    return out


class IsotonicCalibrator:
    """Monotone map raw_score -> calibrated PD, fit by PAVA then interpolated.

    tail: what to do where isotonic carries no usable information.
      "clamp"     -- hold the boundary PD flat (legacy BUILD <=16). No PD floor.
      "loglinear" -- BUILD 17. Outside [x_min, x_max] only, extend the average
                     log-odds trend so an applicant stronger than anyone in the
                     calibration set is not pinned to the boundary PD.
      "hybrid"    -- BUILD 18 default. As loglinear, and ALSO replaces a
                     terminal PLATEAU inside the support (see break_plateau).

    break_plateau: whether the backbone may override a terminal isotonic
      plateau. This is a POLICY CHOICE, deliberately not an automatic one --
      see _plateau_z for why the obvious statistical tests do not work. True
      de-caps the credit score; False reproduces BUILD 17. Either way the
      plateau diagnostics are reported by diagnose() so the decision is
      auditable, MoC is applied wherever the backbone prices, and central
      tendency is re-anchored.
    """

    def __init__(self,
                 tail: str = "hybrid",
                 pd_floor: float = BASEL_PD_FLOOR,
                 min_plateau_knots: int = 2,
                 break_plateau: bool = True,
                 moc_logodds: float = 0.0) -> None:
        self.x_: np.ndarray = np.array([0.0, 1.0])
        self.y_: np.ndarray = np.array([0.0, 1.0])
        self.n_: np.ndarray = np.array([1.0, 1.0])   # observations per knot
        self.k_: np.ndarray = np.array([0.0, 1.0])   # defaults per knot
        self.tail: str = tail
        self.pd_floor: float = float(pd_floor)
        self.min_plateau_knots: int = int(min_plateau_knots)
        self.break_plateau: bool = bool(break_plateau)
        self.moc_logodds: float = float(moc_logodds)
        self.backbone_: Optional[Tuple[float, float]] = None
        self.lo_trend_z_: float = 0.0
        self.hi_trend_z_: float = 0.0
        self.ct_shift_: float = 0.0

    # ------------------------------------------------------------------ fit

    def fit(self, scores, targets) -> "IsotonicCalibrator":
        s = np.asarray(scores, dtype=float)
        y = np.asarray(targets, dtype=float)
        m = ~(np.isnan(s) | np.isnan(y))
        s, y = s[m], y[m]
        if len(s) == 0:
            return self
        order = np.argsort(s, kind="mergesort")
        s_sorted = s[order]
        y_sorted = y[order]
        fitted = _pava(y_sorted, np.ones_like(s_sorted))
        # collapse tied x-values to a single breakpoint (keep the pooled value)
        xs: List[float] = []
        ys: List[float] = []
        ns: List[float] = []
        ks: List[float] = []
        i = 0
        n = len(s_sorted)
        while i < n:
            j = i
            while j + 1 < n and s_sorted[j + 1] == s_sorted[i]:
                j += 1
            xs.append(float(s_sorted[i]))
            ys.append(float(fitted[j]))
            ns.append(float(j - i + 1))
            ks.append(float(y_sorted[i:j + 1].sum()))
            i = j + 1
        self.x_ = np.asarray(xs, dtype=float)
        self.y_ = np.clip(np.asarray(ys, dtype=float), 0.0, 1.0)
        self.n_ = np.asarray(ns, dtype=float)
        self.k_ = np.asarray(ks, dtype=float)
        self.ct_shift_ = 0.0
        self.backbone_ = self._fit_backbone()
        lead, trail = self._terminal_runs()
        self.lo_trend_z_ = self._plateau_z(0, lead)
        self.hi_trend_z_ = self._plateau_z(trail, len(self.x_) - 1)
        return self

    def _fit_backbone(self, mask: Optional[np.ndarray] = None) -> Optional[Tuple[float, float]]:
        """Weighted least squares of logit(observed rate) on score, across knots.

        This is the trend the model ACTUALLY exhibits over the region where the
        data can support an estimate. Rates are Jeffreys-smoothed so that empty
        or fully-clean knots do not blow up the logit. Returns (intercept,
        slope), or None when there is nothing sane to fit.
        """
        x, nn, kk = self.x_, self.n_, self.k_
        if x.size < 3 or nn.size != x.size or kk.size != x.size:
            return None
        if mask is not None:
            x, nn, kk = x[mask], nn[mask], kk[mask]
            if x.size < 3:
                return None
        p = (kk + 0.5) / (nn + 1.0)
        L = self._logit(p)
        W = float(nn.sum())
        if W <= 0.0:
            return None
        xbar = float((nn * x).sum() / W)
        Lbar = float((nn * L).sum() / W)
        sxx = float((nn * (x - xbar) ** 2).sum())
        if sxx <= 0.0:
            return None
        slope = float((nn * (x - xbar) * (L - Lbar)).sum() / sxx)
        if slope <= 0.0:
            return None  # no usable upward trend; do not invent one
        return (Lbar - slope * xbar, slope)

    def _terminal_runs(self, tol: float = 1e-12) -> Tuple[int, int]:
        """Index of the last knot of the leading flat run, and the first knot of
        the trailing flat run."""
        y = self.y_
        n = len(y)
        if n == 0:
            return 0, 0
        lead = 0
        while lead + 1 < n and abs(y[lead + 1] - y[0]) <= tol:
            lead += 1
        trail = n - 1
        while trail - 1 >= 0 and abs(y[trail - 1] - y[n - 1]) <= tol:
            trail -= 1
        return lead, trail

    def _plateau_z(self, i0: int, i1: int) -> float:
        """DIAGNOSTIC ONLY: how far a terminal plateau sits above the trend.

        Is a terminal plateau a POOLING ARTEFACT or a REAL flat region?

        PAVA flattens a run whenever the OBSERVED rates wobble non-monotonically,
        which happens for two very different reasons:
          * the true PD keeps falling but only gently, so per-bin sampling noise
            swamps the step-to-step trend -> the plateau is an artefact, and
            extrapolating through it is the right thing to do;
          * the true PD really is flat there -> extrapolating through it invents
            risk differentiation that does not exist, and WORSENS calibration.

        The obvious test -- regress the raw rates on the score INSIDE the run --
        is invalid. Conditioning on "PAVA chose to pool this block" selects for
        blocks whose observed slope is flat or negative, so that statistic is
        biased downward by construction and essentially never fires. (Measured:
        it returned z = -1.94 on a fixture built with a genuine positive trend.)

        Testing LEVELS against an EXTERNAL trend -- refit the backbone EXCLUDING
        the plateau, extrapolate it across the plateau, compare expected against
        observed defaults -- fixes the selection bias and is well powered. But
        it is defeated by MISSPECIFICATION: a log-linear backbone extrapolated
        into the tail systematically under-predicts, so observed exceeds
        expected almost always. (Measured: z = +20.5 on a fixture whose tail is
        genuinely flat, but ALSO z = +4.3 on one built as a pure artefact. It
        cannot separate the two cases.)

        Conclusion, stated plainly rather than papered over: with this data you
        cannot reliably distinguish a pooling artefact from a real PD floor from
        the calibration sample alone. So the override is a documented policy
        switch (break_plateau), and this statistic is REPORTED, not acted on.
        Read it as "how much higher the plateau sits than a continued log-linear
        trend" -- large values mean more of the de-capping is model assumption
        rather than observed data, so lean on the MoC.
        """
        if i1 <= i0:
            return 0.0
        n_knots = len(self.x_)
        if n_knots < 6 or self.n_.size != n_knots or self.k_.size != n_knots:
            return 0.0
        keep = np.ones(n_knots, dtype=bool)
        keep[i0:i1 + 1] = False
        if int(keep.sum()) < 3:
            return 0.0
        bb = self._fit_backbone(mask=keep)
        if bb is None:
            return 0.0
        a, b = bb
        x = self.x_[i0:i1 + 1]
        nn = self.n_[i0:i1 + 1]
        kk = self.k_[i0:i1 + 1]
        p_pred = _sigmoid(a + b * x)
        expected = float((nn * p_pred).sum())
        observed = float(kk.sum())
        var = float((nn * p_pred * (1.0 - p_pred)).sum())
        if var <= 0.0 or not np.isfinite(var):
            return 0.0
        return (observed - expected) / float(np.sqrt(var))

    # -------------------------------------------------------------- predict

    @staticmethod
    def _logit(p: np.ndarray) -> np.ndarray:
        p = np.clip(np.asarray(p, dtype=float), 1e-9, 1.0 - 1e-9)
        return np.log(p / (1.0 - p))

    def _regions(self, s: np.ndarray):
        """Which scores get priced off the backbone, and where each end joins.

        Returns (lo_mask, lo_x, lo_pd, hi_mask, hi_x, hi_pd) where lo_x/lo_pd is
        the point the backbone must pass through so the curve stays continuous.
        """
        x, y = self.x_, self.y_
        n = len(x)
        lead, trail = self._terminal_runs()
        brk = bool(getattr(self, "break_plateau", True))
        lo_ok = brk and (lead + 1 >= self.min_plateau_knots) and lead < n - 1
        hi_ok = brk and ((n - trail) >= self.min_plateau_knots) and trail > 0
        lo_i = lead if lo_ok else 0
        hi_i = trail if hi_ok else n - 1
        lo_mask = s < x[lo_i]
        hi_mask = s > x[hi_i]
        return lo_mask, float(x[lo_i]), float(y[lo_i]), hi_mask, float(x[hi_i]), float(y[hi_i])

    def _extrapolate(self, s: np.ndarray, within: np.ndarray) -> np.ndarray:
        """BUILD 17 path: outside [x_min, x_max] only, replace the flat clamp
        with a monotone log-odds LINEAR extension of the calibration curve."""
        x0, x1 = float(self.x_[0]), float(self.x_[-1])
        if x1 <= x0:
            return within
        L0 = float(self._logit(np.asarray([self.y_[0]]))[0])
        L1 = float(self._logit(np.asarray([self.y_[-1]]))[0])
        slope = (L1 - L0) / (x1 - x0)
        if slope < 0.0:
            slope = 0.0
        out = np.array(within, dtype=float)
        lo = s < x0
        if np.any(lo):
            out[lo] = _sigmoid(L0 + slope * (s[lo] - x0))
        hi = s > x1
        if np.any(hi):
            out[hi] = _sigmoid(L1 + slope * (s[hi] - x1))
        return out

    def _predict_raw(self, scores, apply_ct: bool = True):
        """Returns (pd, replaced_mask). replaced_mask flags rows priced off the
        extrapolated backbone rather than off observed isotonic bins."""
        s = np.asarray(scores, dtype=float)
        tail = getattr(self, "tail", "hybrid")
        if len(self.x_) == 1:
            return np.full_like(s, float(self.y_[0])), np.zeros_like(s, dtype=bool)
        out = np.interp(s, self.x_, self.y_, left=self.y_[0], right=self.y_[-1])
        replaced = np.zeros_like(s, dtype=bool)

        if tail == "clamp":
            return np.clip(out, 0.0, 1.0), replaced  # legacy: no PD floor

        floor = float(getattr(self, "pd_floor", BASEL_PD_FLOOR))
        bb = getattr(self, "backbone_", None)

        if tail == "hybrid" and bb is not None:
            a, b = bb
            lo_mask, lo_x, lo_pd, hi_mask, hi_x, hi_pd = self._regions(s)
            # Anchor the backbone so it passes exactly through the join point.
            # Without this the two pieces meet at a step, which can break
            # monotonicity and therefore the ranking.
            if np.any(lo_mask):
                anchor = float(self._logit(np.asarray([lo_pd]))[0]) - (a + b * lo_x)
                out[lo_mask] = _sigmoid(a + b * s[lo_mask] + anchor)
                replaced |= lo_mask
            if np.any(hi_mask):
                anchor = float(self._logit(np.asarray([hi_pd]))[0]) - (a + b * hi_x)
                out[hi_mask] = _sigmoid(a + b * s[hi_mask] + anchor)
                replaced |= hi_mask
        else:
            # loglinear, or hybrid with no usable backbone -> degrade safely
            before = out.copy()
            out = self._extrapolate(s, out)
            replaced = ~np.isclose(out, before)

        # Margin of Conservatism: applied ONLY where we extrapolated. Pricing
        # off an extrapolated trend is estimation beyond observed support, which
        # under Basel requires a documented MoC (category C, general estimation
        # error). Never applied to the observed interior.
        moc = float(getattr(self, "moc_logodds", 0.0))
        if moc != 0.0 and np.any(replaced):
            out[replaced] = _sigmoid(self._logit(out[replaced]) + moc)

        # Central tendency: mean predicted PD must equal the observed long-run
        # average default rate. Monotone in the score, so Gini is unaffected.
        ct = float(getattr(self, "ct_shift_", 0.0))
        if apply_ct and ct != 0.0:
            out = _sigmoid(self._logit(out) + ct)

        return np.clip(out, floor, 1.0 - floor), replaced

    def predict(self, scores) -> np.ndarray:
        return self._predict_raw(scores)[0]

    def fit_central_tendency(self, scores, target_rate: float,
                             tol: float = 1e-9, max_iter: int = 200) -> float:
        """Re-anchor so mean predicted PD == target_rate (the observed long-run
        default rate). Bisection on a shift in log-odds space."""
        s = np.asarray(scores, dtype=float)
        target = float(target_rate)
        self.ct_shift_ = 0.0
        if s.size == 0 or not (0.0 < target < 1.0):
            return 0.0
        base, _ = self._predict_raw(s, apply_ct=False)
        base_logit = self._logit(base)

        def mean_at(c: float) -> float:
            return float(np.mean(_sigmoid(base_logit + c)))

        lo, hi = -20.0, 20.0
        if mean_at(lo) > target or mean_at(hi) < target:
            return 0.0
        for _ in range(max_iter):
            mid = 0.5 * (lo + hi)
            if mean_at(mid) < target:
                lo = mid
            else:
                hi = mid
            if hi - lo < tol:
                break
        self.ct_shift_ = 0.5 * (lo + hi)
        return self.ct_shift_

    def diagnose(self, scores) -> dict:
        """Everything you need to justify (or reject) the tail treatment."""
        s = np.asarray(scores, dtype=float)
        pd, replaced = self._predict_raw(s)
        lead, trail = self._terminal_runs()
        bb = getattr(self, "backbone_", None)
        return {
            "n": int(s.size),
            "frac_backbone_priced": float(replaced.mean()) if s.size else 0.0,
            "frac_below_support": float((s < self.x_[0]).mean()) if s.size else 0.0,
            "frac_above_support": float((s > self.x_[-1]).mean()) if s.size else 0.0,
            "lower_plateau_knots": float(lead + 1),
            "upper_plateau_knots": float(len(self.x_) - trail),
            "isotonic_pd_min": float(self.y_[0]),
            "isotonic_pd_max": float(self.y_[-1]),
            "pd_min": float(pd.min()) if s.size else float("nan"),
            "pd_max": float(pd.max()) if s.size else float("nan"),
            "mean_pd": float(pd.mean()) if s.size else float("nan"),
            "ct_shift": float(getattr(self, "ct_shift_", 0.0)),
            "moc_logodds": float(getattr(self, "moc_logodds", 0.0)),
            "backbone_slope": float(bb[1]) if bb is not None else float("nan"),
            "lower_plateau_z": float(getattr(self, "lo_trend_z_", 0.0)),
            "upper_plateau_z": float(getattr(self, "hi_trend_z_", 0.0)),
            "break_plateau": bool(getattr(self, "break_plateau", True)),
        }

    # ----------------------------------------------------------- persistence

    def save(self, path: str) -> None:
        with open(path, "wb") as f:
            pickle.dump({"x": self.x_.tolist(), "y": self.y_.tolist(),
                         "n": self.n_.tolist(), "k": self.k_.tolist(),
                         "tail": self.tail, "pd_floor": self.pd_floor,
                         "min_plateau_knots": self.min_plateau_knots,
                         "break_plateau": self.break_plateau,
                         "lo_trend_z": self.lo_trend_z_,
                         "hi_trend_z": self.hi_trend_z_,
                         "moc_logodds": self.moc_logodds,
                         "backbone": self.backbone_,
                         "ct_shift": self.ct_shift_}, f)

    @classmethod
    def load(cls, path: str) -> "IsotonicCalibrator":
        with open(path, "rb") as f:
            d = pickle.load(f)
        obj = cls(tail=d.get("tail", "hybrid"),
                  pd_floor=float(d.get("pd_floor", BASEL_PD_FLOOR)),
                  min_plateau_knots=int(d.get("min_plateau_knots", 2)),
                  break_plateau=bool(d.get("break_plateau", True)),
                  moc_logodds=float(d.get("moc_logodds", 0.0)))
        obj.x_ = np.asarray(d["x"], dtype=float)
        obj.y_ = np.asarray(d["y"], dtype=float)
        # BUILD <=17 pickles carry no per-knot counts, so no backbone can be
        # fitted. Fall back to the loglinear path rather than guessing.
        if "n" in d and "k" in d:
            obj.n_ = np.asarray(d["n"], dtype=float)
            obj.k_ = np.asarray(d["k"], dtype=float)
        else:
            obj.n_ = np.array([])
            obj.k_ = np.array([])
        bb = d.get("backbone", None)
        obj.backbone_ = tuple(bb) if bb is not None else None
        obj.lo_trend_z_ = float(d.get("lo_trend_z", 0.0))
        obj.hi_trend_z_ = float(d.get("hi_trend_z", 0.0))
        obj.ct_shift_ = float(d.get("ct_shift", 0.0))
        return obj


def brier_score(pd_pred, y_true) -> float:
    p = np.asarray(pd_pred, dtype=float)
    y = np.asarray(y_true, dtype=float)
    m = ~(np.isnan(p) | np.isnan(y))
    p, y = p[m], y[m]
    if len(p) == 0:
        return float("nan")
    return float(np.mean((p - y) ** 2))


def expected_calibration_error(pd_pred, y_true, n_bins: int = 20) -> float:
    """Equal-COUNT binned |mean predicted - observed rate|, weighted by bin size.

    Equal-count rather than equal-width, because with a mean PD near 0.24 the
    upper equal-width bins are nearly empty and the statistic becomes noise.
    """
    p = np.asarray(pd_pred, dtype=float)
    y = np.asarray(y_true, dtype=float)
    m = ~(np.isnan(p) | np.isnan(y))
    p, y = p[m], y[m]
    if len(p) == 0:
        return float("nan")
    order = np.argsort(p, kind="mergesort")
    p, y = p[order], y[order]
    total = 0.0
    for pb, yb in zip(np.array_split(p, n_bins), np.array_split(y, n_bins)):
        if pb.size == 0:
            continue
        total += pb.size * abs(float(pb.mean()) - float(yb.mean()))
    return total / float(len(p))


def reliability_table(pd_pred, y_true, n_bins: int = 10) -> List[Tuple[int, int, float, float]]:
    """[(bin_index, count, mean_pred, observed_rate)] over equal-width [0,1] bins."""
    p = np.asarray(pd_pred, dtype=float)
    y = np.asarray(y_true, dtype=float)
    m = ~(np.isnan(p) | np.isnan(y))
    p, y = p[m], y[m]
    out: List[Tuple[int, int, float, float]] = []
    if len(p) == 0:
        return out
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    for b in range(n_bins):
        lo, hi = edges[b], edges[b + 1]
        if b == n_bins - 1:
            mask = (p >= lo) & (p <= hi)
        else:
            mask = (p >= lo) & (p < hi)
        cnt = int(mask.sum())
        if cnt == 0:
            out.append((b, 0, float("nan"), float("nan")))
        else:
            out.append((b, cnt, float(p[mask].mean()), float(y[mask].mean())))
    return out
